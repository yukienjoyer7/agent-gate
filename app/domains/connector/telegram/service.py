from __future__ import annotations

import asyncio
import hmac
import logging
import re
import threading
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

from app.config.settings import Settings, get_settings
from app.core.run_schema import RunStatus, StepStatus
from app.core.schemas import ExecutionStatus, new_id
from app.domains.agent.services.run_registry import (
    RunRegistry,
    RunSession,
    StepState,
    run_registry,
)
from app.domains.agent.services.run_service import is_terminal, start_agent_run
from app.domains.connector.telegram.contacts import (
    TelegramContactRepository,
    TelegramContactStore,
    build_display_name,
)
from app.domains.connector.telegram.telegram import TelegramConnector

logger = logging.getLogger(__name__)

CallbackDecision = str

_CALLBACK_RE = re.compile(r"^ag:(approve|decline):([A-Za-z0-9_:-]+):(\d+)$")
_TERMINAL_STATUSES = {
    RunStatus.DONE,
    RunStatus.FAILED,
    RunStatus.BLOCKED,
    RunStatus.DECLINED,
    RunStatus.ERROR,
    RunStatus.CANCELLED,
}


class TelegramWebhookAuthError(Exception):
    """Raised when a webhook request fails Telegram secret validation."""


class TelegramWebhookMisconfiguredError(Exception):
    """Raised when Telegram webhook verification cannot be performed safely."""


class BoundedDeduplicator:
    """Small bounded in-memory duplicate tracker for Telegram update/callback ids."""

    def __init__(self, max_size: int = 1000) -> None:
        self._max_size = max(1, max_size)
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self._lock = threading.Lock()

    def is_duplicate_or_mark(self, key: Any) -> bool:
        if key is None or str(key).strip() == "":
            return False
        normalized = str(key)
        with self._lock:
            if normalized in self._seen:
                return True
            self._seen.add(normalized)
            self._order.append(normalized)
            while len(self._order) > self._max_size:
                oldest = self._order.popleft()
                self._seen.discard(oldest)
        return False


class TelegramService:
    """Telegram channel adapter for inbound updates and approval callbacks."""

    def __init__(
        self,
        *,
        connector: TelegramConnector | None = None,
        registry: RunRegistry = run_registry,
        settings_factory: Callable[[], Settings] = get_settings,
        start_run: Callable[..., RunSession] | None = None,
        background_tasks: bool = True,
        poll_interval_sec: float = 0.5,
        dedupe_size: int = 1000,
        contact_repository: TelegramContactStore | None = None,
    ) -> None:
        self._connector = connector or TelegramConnector()
        self._registry = registry
        self._settings_factory = settings_factory
        self._start_run = start_run
        self._background_tasks = background_tasks
        self._poll_interval_sec = poll_interval_sec
        self._updates = BoundedDeduplicator(dedupe_size)
        self._approval_callbacks = BoundedDeduplicator(dedupe_size)
        self._contacts = contact_repository or TelegramContactRepository()

    def validate_webhook_secret(self, received_secret: str | None) -> None:
        expected_secret = self._settings_factory().TELEGRAM_WEBHOOK_SECRET
        if not expected_secret:
            raise TelegramWebhookMisconfiguredError("Telegram webhook secret is not configured")
        if not received_secret:
            raise TelegramWebhookAuthError("missing Telegram webhook secret")
        if not hmac.compare_digest(received_secret, expected_secret):
            raise TelegramWebhookAuthError("invalid Telegram webhook secret")

    async def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(update, dict):
            logger.info("Telegram update ignored", extra={"reason": "malformed"})
            return {"ok": True, "status": "ignored"}

        update_id = update.get("update_id")
        if self._updates.is_duplicate_or_mark(update_id):
            logger.info("Telegram duplicate update ignored", extra={"update_id": update_id})
            return {"ok": True, "status": "duplicate"}

        logger.info("Telegram update received", extra={"update_id": update_id})

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            return await self._handle_callback_query(callback_query, update_id)

        message = update.get("message")
        if not isinstance(message, dict):
            logger.info(
                "Telegram update ignored", extra={"update_id": update_id, "reason": "no_message"}
            )
            return {"ok": True, "status": "ignored"}

        # Registration is an inbound-channel concern, not a side effect of a
        # later agent action. A /start message is therefore enough for the
        # bot to learn a contact's address for future guarded sends.
        await self._register_contact(message, update_id)

        inbound = _extract_text_message(message, update_id)
        if inbound is None:
            logger.info(
                "Telegram update ignored",
                extra={"update_id": update_id, "reason": "unsupported_message"},
            )
            return {"ok": True, "status": "ignored"}

        run = self._create_run(inbound)
        logger.info(
            "Telegram run created",
            extra={"update_id": update_id, "run_id": run.run_id, "chat_id": inbound["chat_id"]},
        )
        if self._background_tasks:
            self._spawn(self._deliver_run_lifecycle(run, inbound))
        return {"ok": True, "status": "accepted", "run_id": run.run_id}

    async def _register_contact(self, message: dict[str, Any], update_id: Any) -> None:
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            return
        chat_id = _coerce_chat_id(chat.get("id"))
        chat_type = chat.get("type")
        if chat_id is None or not isinstance(chat_type, str) or not chat_type.strip():
            logger.info(
                "Telegram contact ignored",
                extra={"update_id": update_id, "reason": "invalid_chat_identity"},
            )
            return

        first_name = _optional_text(chat.get("first_name"))
        last_name = _optional_text(chat.get("last_name"))
        username = _optional_text(chat.get("username"))
        display_name = build_display_name(
            first_name,
            last_name,
            fallback=_optional_text(chat.get("title")) or username,
        )
        try:
            await self._contacts.upsert(
                chat_id=chat_id,
                chat_type=chat_type.strip(),
                username=username,
                first_name=first_name,
                last_name=last_name,
                display_name=display_name,
            )
        except Exception:  # noqa: BLE001 - registration must not block inbound processing
            logger.warning(
                "Telegram contact registration failed",
                extra={"update_id": update_id, "chat_id": chat_id},
            )
            return
        logger.info(
            "Telegram contact registered",
            extra={"update_id": update_id, "chat_id": chat_id, "chat_type": chat_type},
        )

    def _create_run(self, inbound: dict[str, Any]) -> RunSession:
        metadata = {
            "update_id": inbound.get("update_id"),
            "message_id": inbound.get("message_id"),
            "user_id": inbound.get("user_id"),
            "chat_type": inbound.get("chat_type"),
        }
        metadata = {key: value for key, value in metadata.items() if value is not None}
        if self._start_run is not None:
            return self._start_run(
                inbound["text"],
                channel="telegram",
                channel_id=str(inbound["chat_id"]),
                metadata=metadata,
            )
        return start_agent_run(
            inbound["text"],
            channel="telegram",
            channel_id=str(inbound["chat_id"]),
            metadata=metadata,
            registry=self._registry,
        )

    async def _handle_callback_query(
        self, callback_query: dict[str, Any], update_id: Any
    ) -> dict[str, Any]:
        callback_query_id = str(callback_query.get("id") or "")
        parsed = _parse_callback_data(callback_query.get("data"))
        if parsed is None:
            await self._answer_callback(callback_query_id, "Unsupported AgentGate callback")
            logger.info(
                "Telegram callback rejected",
                extra={"update_id": update_id, "reason": "invalid_callback_data"},
            )
            return {"ok": True, "status": "callback_ignored"}

        decision, run_id, step_index = parsed
        run = self._registry.get(run_id)
        if run is None:
            await self._answer_callback(callback_query_id, "Run not found")
            logger.info(
                "Telegram callback rejected",
                extra={"update_id": update_id, "run_id": run_id, "reason": "run_not_found"},
            )
            return {"ok": True, "status": "callback_rejected"}

        message_context = _callback_message_context(callback_query)
        if not _callback_matches_run(run, callback_query, message_context):
            await self._answer_callback(callback_query_id, "This approval is not valid here")
            logger.info(
                "Telegram callback rejected",
                extra={"update_id": update_id, "run_id": run_id, "reason": "context_mismatch"},
            )
            return {"ok": True, "status": "callback_rejected"}

        callback_key = f"{run_id}:{step_index}"
        if self._approval_callbacks.is_duplicate_or_mark(callback_key):
            await self._answer_callback(callback_query_id, "Approval already handled")
            await self._clear_callback_keyboard(message_context, run.run_id)
            return {"ok": True, "status": "callback_duplicate"}

        try:
            self._registry.respond(run, step_index, decision)
        except LookupError:
            await self._answer_callback(callback_query_id, "Step not found")
            await self._clear_callback_keyboard(message_context, run.run_id)
            logger.info(
                "Telegram callback rejected",
                extra={"update_id": update_id, "run_id": run_id, "reason": "step_not_found"},
            )
            return {"ok": True, "status": "callback_rejected"}
        except ValueError:
            await self._answer_callback(callback_query_id, "Approval is no longer pending")
            await self._clear_callback_keyboard(message_context, run.run_id)
            logger.info(
                "Telegram callback rejected",
                extra={"update_id": update_id, "run_id": run_id, "reason": "not_waiting"},
            )
            return {"ok": True, "status": "callback_rejected"}

        await self._answer_callback(
            callback_query_id,
            "Approved" if decision == "approve" else "Declined",
        )
        await self._clear_callback_keyboard(message_context, run.run_id)
        logger.info(
            "Telegram callback accepted",
            extra={"update_id": update_id, "run_id": run_id, "step_index": step_index},
        )
        return {
            "ok": True,
            "status": "callback_accepted",
            "run_id": run_id,
            "step_index": step_index,
        }

    async def _deliver_run_lifecycle(self, run: RunSession, inbound: dict[str, Any]) -> None:
        chat_id = inbound["chat_id"]
        await self._send_message(
            chat_id,
            f"Request received. Run ID: {run.run_id}",
            run_id=run.run_id,
            reply_to_message_id=inbound.get("message_id"),
        )

        approval_notified: set[int] = set()
        input_notified: set[int] = set()
        while True:
            await self._notify_waiting_steps(run, chat_id, approval_notified, input_notified)
            if is_terminal(run):
                await self._send_message(
                    chat_id,
                    _format_final_message(run),
                    run_id=run.run_id,
                    reply_to_message_id=inbound.get("message_id"),
                )
                return
            await asyncio.sleep(self._poll_interval_sec)

    async def _notify_waiting_steps(
        self,
        run: RunSession,
        chat_id: Any,
        approval_notified: set[int],
        input_notified: set[int],
    ) -> None:
        for step in run.steps:
            if step.status == StepStatus.WAITING_APPROVAL and step.index not in approval_notified:
                approval_notified.add(step.index)
                await self._send_message(
                    chat_id,
                    _format_approval_message(run, step),
                    run_id=run.run_id,
                    reply_markup=_approval_reply_markup(run.run_id, step.index),
                )
            elif step.status == StepStatus.WAITING_INPUT and step.index not in input_notified:
                input_notified.add(step.index)
                await self._send_message(
                    chat_id,
                    _format_input_required_message(run, step),
                    run_id=run.run_id,
                )

    async def _send_message(
        self,
        chat_id: Any,
        text: str,
        *,
        run_id: str,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: Any = None,
    ) -> None:
        payload: dict[str, Any] = {
            "run_id": run_id,
            "action_id": new_id("act"),
            "chat_id": chat_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        result = await self._connector.execute("send_message", payload)
        if result.status == ExecutionStatus.SUCCESS:
            logger.info(
                "Telegram message sent",
                extra={
                    "run_id": run_id,
                    "chat_id": chat_id,
                    "message_id": result.data.get("message_id"),
                },
            )
        else:
            logger.warning(
                "Telegram API failed",
                extra={"run_id": run_id, "chat_id": chat_id, "error": result.error},
            )

    async def _answer_callback(self, callback_query_id: str, text: str) -> None:
        if not callback_query_id:
            return
        result = await self._connector.execute(
            "answer_callback_query",
            {
                "run_id": "telegram_callback",
                "action_id": new_id("act"),
                "callback_query_id": callback_query_id,
                "text": text,
            },
        )
        if result.status != ExecutionStatus.SUCCESS:
            logger.warning("Telegram API failed", extra={"error": result.error})

    async def _clear_callback_keyboard(self, context: dict[str, Any] | None, run_id: str) -> None:
        if not context:
            return
        chat_id = context.get("chat_id")
        message_id = context.get("message_id")
        if chat_id is None or message_id is None:
            return
        result = await self._connector.execute(
            "edit_message_reply_markup",
            {
                "run_id": run_id,
                "action_id": new_id("act"),
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )
        if result.status != ExecutionStatus.SUCCESS:
            logger.warning(
                "Telegram API failed",
                extra={"run_id": run_id, "chat_id": chat_id, "error": result.error},
            )

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task: asyncio.Task[Any] = asyncio.create_task(coroutine)
        task.add_done_callback(_log_task_failure)


def _extract_text_message(message: dict[str, Any], update_id: Any) -> dict[str, Any] | None:
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None
    sender = message.get("from")
    user_id = sender.get("id") if isinstance(sender, dict) else None
    return {
        "update_id": update_id,
        "message_id": message.get("message_id"),
        "chat_id": chat["id"],
        "chat_type": chat.get("type"),
        "user_id": user_id,
        "text": text.strip(),
    }


def _coerce_chat_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value != 0 else None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        parsed = int(value.strip())
        return parsed if parsed != 0 else None
    return None


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_callback_data(data: Any) -> tuple[CallbackDecision, str, int] | None:
    if not isinstance(data, str):
        return None
    match = _CALLBACK_RE.match(data.strip())
    if not match:
        return None
    decision, run_id, raw_step_index = match.groups()
    try:
        step_index = int(raw_step_index)
    except ValueError:
        return None
    return decision, run_id, step_index


def _callback_message_context(callback_query: dict[str, Any]) -> dict[str, Any] | None:
    message = callback_query.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    return {"chat_id": chat.get("id"), "message_id": message.get("message_id")}


def _callback_matches_run(
    run: RunSession,
    callback_query: dict[str, Any],
    context: dict[str, Any] | None,
) -> bool:
    if run.channel != "telegram":
        return False
    if context is None or str(context.get("chat_id") or "") != str(run.channel_id or ""):
        return False
    original_user_id = run.metadata.get("user_id")
    from_user = callback_query.get("from")
    callback_user_id = from_user.get("id") if isinstance(from_user, dict) else None
    if original_user_id is not None and callback_user_id is not None:
        return str(original_user_id) == str(callback_user_id)
    return True


def _approval_reply_markup(run_id: str, step_index: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"ag:approve:{run_id}:{step_index}"},
                {"text": "❌ Reject", "callback_data": f"ag:decline:{run_id}:{step_index}"},
            ]
        ]
    }


def _format_approval_message(run: RunSession, step: StepState) -> str:
    public = step.public()
    reasons = []
    if isinstance(step.decision, dict):
        reasons = [str(reason) for reason in step.decision.get("reasons") or []]
    reason = reasons[0] if reasons else "This action requires approval."
    lines = [
        "Approval required",
        f"Run ID: {run.run_id}",
        f"Step: {step.index}",
        f"Action: {public.get('action_type', 'unknown')}",
        f"Target system: {public.get('target_system', 'unknown')}",
    ]
    target = str(public.get("target") or "").strip()
    if target:
        lines.append(f"Target: {target[:200]}")
    lines.append(f"Reason: {reason[:250]}")
    return "\n".join(lines)


def _format_input_required_message(run: RunSession, step: StepState) -> str:
    fields = step.sanitize_fields or []
    labels = ", ".join(str(field.get("label") or field.get("key")) for field in fields)
    field_text = f" Required: {labels}." if labels else ""
    return (
        f"Additional input is required for Run ID: {run.run_id}, step {step.index}."
        f"{field_text} Open AgentGate and respond to the paused run to continue."
    )


def _format_final_message(run: RunSession) -> str:
    summary = _latest_result_summary(run)
    status = run.status.value
    if run.status == RunStatus.DONE:
        prefix = "Run completed"
    elif run.status == RunStatus.BLOCKED:
        prefix = "Run blocked"
    elif run.status == RunStatus.DECLINED:
        prefix = "Run declined"
    elif run.status in _TERMINAL_STATUSES:
        prefix = "Run finished"
    else:
        prefix = "Run status"
    if summary:
        return f"{prefix}. Run ID: {run.run_id}. Status: {status}.\n{summary}"
    return f"{prefix}. Run ID: {run.run_id}. Status: {status}."


def _latest_result_summary(run: RunSession) -> str:
    for step in reversed(run.steps):
        execution = step.execution or {}
        summary = execution.get("result_summary")
        if summary:
            return str(summary)[:1500]
    if run.last_observation:
        return run.last_observation[:1500]
    return ""


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Telegram background task failed")
