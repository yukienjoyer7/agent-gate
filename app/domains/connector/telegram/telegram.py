from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from app.config.settings import get_settings
from app.core.errors import ConnectorError, ConnectorErrorCode
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector

logger = logging.getLogger(__name__)

_TELEGRAM_BOT_URL_RE = re.compile(r"(https?://[^/\s]+/bot)[^/\s]+", re.IGNORECASE)


class _TelegramHTTPLogRedactionFilter(logging.Filter):
    """Redact Bot API URL credentials while preserving unrelated httpx logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        sanitized = _TELEGRAM_BOT_URL_RE.sub(r"\1[REDACTED]", message)
        if sanitized != message:
            record.msg = sanitized
            record.args = ()
        return True


def _install_httpx_telegram_redaction() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(item, _TelegramHTTPLogRedactionFilter) for item in httpx_logger.filters):
        httpx_logger.addFilter(_TelegramHTTPLogRedactionFilter())


_install_httpx_telegram_redaction()

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_SAFE_TEXT_LIMIT = 4000
SUPPORTED_TELEGRAM_ACTIONS = {
    "send_message",
    "answer_callback_query",
    "edit_message_reply_markup",
}


@dataclass
class TelegramAPIError(Exception):
    message: str
    code: ConnectorErrorCode = ConnectorErrorCode.UNKNOWN
    retryable: bool = False
    details: dict[str, Any] | None = None


class TelegramConnector(BaseConnector):
    """Telegram Bot API connector.

    Agent-planned Telegram actions still enter through ActionRequest ->
    Guardrail -> ExecutionRouter -> APIExecutor before reaching this connector.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        token: str | None = None,
        api_base: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._token = token
        self._api_base = api_base
        self._timeout = timeout

    async def execute(self, action: str, payload: dict[str, Any]) -> ExecutionResult:
        started = perf_counter()
        run_id = str(payload.get("run_id") or "")
        action_id = str(payload.get("action_id") or "")

        if action not in SUPPORTED_TELEGRAM_ACTIONS:
            return failed(
                run_id,
                action_id,
                "unsupported Telegram action",
                latency_ms=_elapsed_ms(started),
            )

        token = self._token if self._token is not None else get_settings().TELEGRAM_BOT_TOKEN
        if not token:
            return failed(
                run_id,
                action_id,
                "Telegram bot token is not configured",
                ConnectorErrorCode.AUTH,
                latency_ms=_elapsed_ms(started),
            )

        try:
            if action == "send_message":
                return await self._send_message(payload, token, started)
            if action == "answer_callback_query":
                return await self._answer_callback_query(payload, token, started)
            if action == "edit_message_reply_markup":
                return await self._edit_message_reply_markup(payload, token, started)
        except TelegramAPIError as exc:
            return failed(
                run_id,
                action_id,
                _redact_token(exc.message, token),
                exc.code,
                retryable=exc.retryable,
                details=exc.details or {},
                latency_ms=_elapsed_ms(started),
            )
        except httpx.TimeoutException:
            return failed(
                run_id,
                action_id,
                "Telegram request timed out",
                ConnectorErrorCode.TIMEOUT,
                retryable=True,
                latency_ms=_elapsed_ms(started),
            )
        except httpx.HTTPError:
            return failed(
                run_id,
                action_id,
                "Telegram is unavailable",
                ConnectorErrorCode.UNAVAILABLE,
                retryable=True,
                latency_ms=_elapsed_ms(started),
            )

        raise AssertionError(f"unhandled Telegram action: {action}")

    async def _send_message(
        self, payload: dict[str, Any], token: str, started: float
    ) -> ExecutionResult:
        run_id = str(payload.get("run_id") or "")
        action_id = str(payload.get("action_id") or "")
        chat_id = _numeric_chat_id(payload.get("chat_id"))
        if chat_id is None:
            return failed(
                run_id,
                action_id,
                "chat_id must be a numeric Telegram chat ID",
                latency_ms=_elapsed_ms(started),
            )

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return failed(
                run_id,
                action_id,
                "text is required",
                latency_ms=_elapsed_ms(started),
            )

        base_body: dict[str, Any] = {"chat_id": chat_id}
        for key in (
            "parse_mode",
            "reply_markup",
            "reply_to_message_id",
            "disable_web_page_preview",
            "disable_notification",
        ):
            if key in payload and payload[key] is not None:
                base_body[key] = payload[key]

        message_ids: list[int] = []
        result_chat_id: Any = chat_id
        chunks = split_telegram_text(text)
        for chunk in chunks:
            data = await self._post("sendMessage", {**base_body, "text": chunk}, token)
            result = data.get("result")
            if isinstance(result, dict):
                if isinstance(result.get("message_id"), int):
                    message_ids.append(result["message_id"])
                chat = result.get("chat")
                if isinstance(chat, dict) and chat.get("id") is not None:
                    result_chat_id = chat["id"]

        return ExecutionResult(
            run_id=run_id,
            action_id=action_id,
            executor="telegram",
            status=ExecutionStatus.SUCCESS,
            result_summary="Sent Telegram message",
            data={
                "chat_id": result_chat_id,
                "message_id": message_ids[0] if message_ids else None,
                "message_ids": message_ids,
                "chunks": len(chunks),
            },
            latency_ms=_elapsed_ms(started),
        )

    async def _answer_callback_query(
        self, payload: dict[str, Any], token: str, started: float
    ) -> ExecutionResult:
        run_id = str(payload.get("run_id") or "")
        action_id = str(payload.get("action_id") or "")
        callback_query_id = payload.get("callback_query_id")
        if callback_query_id is None or str(callback_query_id).strip() == "":
            return failed(
                run_id,
                action_id,
                "callback_query_id is required",
                latency_ms=_elapsed_ms(started),
            )

        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        for key in ("text", "show_alert", "url", "cache_time"):
            if key in payload and payload[key] is not None:
                body[key] = payload[key]

        await self._post("answerCallbackQuery", body, token)
        return ExecutionResult(
            run_id=run_id,
            action_id=action_id,
            executor="telegram",
            status=ExecutionStatus.SUCCESS,
            result_summary="Answered Telegram callback query",
            data={"callback_query_id": str(callback_query_id)},
            latency_ms=_elapsed_ms(started),
        )

    async def _edit_message_reply_markup(
        self, payload: dict[str, Any], token: str, started: float
    ) -> ExecutionResult:
        run_id = str(payload.get("run_id") or "")
        action_id = str(payload.get("action_id") or "")
        inline_message_id = payload.get("inline_message_id")
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        if inline_message_id is None and (
            chat_id is None
            or str(chat_id).strip() == ""
            or message_id is None
            or str(message_id).strip() == ""
        ):
            return failed(
                run_id,
                action_id,
                "chat_id and message_id are required",
                latency_ms=_elapsed_ms(started),
            )

        body: dict[str, Any] = {}
        if inline_message_id is not None:
            body["inline_message_id"] = inline_message_id
        else:
            body["chat_id"] = chat_id
            body["message_id"] = message_id
        if "reply_markup" in payload:
            body["reply_markup"] = payload["reply_markup"]

        data = await self._post("editMessageReplyMarkup", body, token)
        result = data.get("result")
        result_message_id = message_id
        result_chat_id = chat_id
        if isinstance(result, dict):
            result_message_id = result.get("message_id", message_id)
            chat = result.get("chat")
            if isinstance(chat, dict):
                result_chat_id = chat.get("id", chat_id)

        return ExecutionResult(
            run_id=run_id,
            action_id=action_id,
            executor="telegram",
            status=ExecutionStatus.SUCCESS,
            result_summary="Updated Telegram message reply markup",
            data={"chat_id": result_chat_id, "message_id": result_message_id},
            latency_ms=_elapsed_ms(started),
        )

    async def _post(self, method: str, body: dict[str, Any], token: str) -> dict[str, Any]:
        path = f"/bot{token}/{method}"
        if self._client is not None:
            response = await self._client.post(path, json=body)
        else:
            settings = get_settings()
            api_base = (self._api_base or settings.TELEGRAM_API_BASE).rstrip("/")
            async with httpx.AsyncClient(base_url=api_base, timeout=self._timeout) as client:
                response = await client.post(path, json=body)
        logger.info("Telegram API request: %s -> HTTP %s", method, response.status_code)
        return _parse_response(response)


def split_telegram_text(text: str, *, limit: int = TELEGRAM_SAFE_TEXT_LIMIT) -> list[str]:
    """Split text under Telegram's message limit, preferring readable breaks."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if split_at < max(1, int(limit * 0.6)):
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise TelegramAPIError(
            f"Telegram returned HTTP {response.status_code}",
            _code_for_status(response.status_code),
            retryable=response.status_code >= 500 or response.status_code == 429,
            details={"status_code": response.status_code},
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramAPIError(
            "Telegram returned malformed JSON",
            ConnectorErrorCode.UNKNOWN,
            details={"status_code": response.status_code},
        ) from exc

    if not isinstance(data, dict):
        raise TelegramAPIError(
            "Telegram returned malformed JSON",
            ConnectorErrorCode.UNKNOWN,
            details={"status_code": response.status_code},
        )

    if data.get("ok") is not True:
        description = str(data.get("description") or "Telegram API returned ok=false")
        raise TelegramAPIError(
            f"Telegram API error: {description[:300]}",
            ConnectorErrorCode.UNKNOWN,
            details={"error_code": data.get("error_code")},
        )

    if "result" not in data:
        raise TelegramAPIError("Telegram returned malformed JSON", ConnectorErrorCode.UNKNOWN)
    return data


def _code_for_status(status_code: int) -> ConnectorErrorCode:
    if status_code == 401:
        return ConnectorErrorCode.AUTH
    if status_code == 403:
        return ConnectorErrorCode.PERMISSION
    if status_code == 404:
        return ConnectorErrorCode.NOT_FOUND
    if status_code == 429:
        return ConnectorErrorCode.RATE_LIMIT
    if status_code >= 500:
        return ConnectorErrorCode.UNAVAILABLE
    return ConnectorErrorCode.UNKNOWN


def failed(
    run_id: str,
    action_id: str,
    message: str,
    code: ConnectorErrorCode = ConnectorErrorCode.VALIDATION,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    latency_ms: int = 0,
) -> ExecutionResult:
    return ExecutionResult(
        run_id=run_id,
        action_id=action_id,
        executor="telegram",
        status=ExecutionStatus.FAILED,
        result_summary=message,
        error=ConnectorError(
            code=code, message=message, retryable=retryable, details=details or {}
        ).model_dump(mode="json"),
        latency_ms=latency_ms,
    )


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def _numeric_chat_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        candidate = int(value.strip())
    else:
        return None
    return candidate if candidate != 0 else None


def _redact_token(message: str, token: str) -> str:
    if not token:
        return message
    return message.replace(token, "[REDACTED]")
