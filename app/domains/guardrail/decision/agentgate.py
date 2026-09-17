"""Adapt the pinned AgentGate engine to the application's action contracts."""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from app.config.settings import get_settings
from app.core.action_request import browser_action_needs_payment_approval, build_action_request
from app.core.schemas import ActionRequest, Decision, DecisionResponse, RiskLevel, new_id
from app.domains.guardrail._vendor.agentgate.audit import AuditUnavailable
from app.domains.guardrail._vendor.agentgate.decision import DecisionEngine
from app.domains.guardrail._vendor.agentgate.detectors import (
    LLMActionIntentDetector,
    LLMPaymentPhishingDetector,
    LLMPIIDetector,
    LLMPromptInjectionDetector,
    LLMSecretDetector,
    LLMSourceCodeDetector,
    LLMUnifiedDetector,
)
from app.domains.guardrail._vendor.agentgate.sanitizer import sanitize
from app.domains.guardrail._vendor.agentgate.schemas import ActionRequest as EngineRequest
from app.domains.guardrail._vendor.agentgate.schemas import DecisionResponse as EngineResponse
from app.domains.guardrail.decision.simple import decide_rule
from app.domains.guardrail.sensitive import is_sensitive_key
from app.runtime.context import current_runtime

# Trusted connector capabilities, independent of planner-supplied risk hints.
# Entries contain risk hints and fields that may be redacted. Routing arguments
# (IDs, recipients, paths, URLs, amounts) are scanned but never rewritten.
_TOOLS: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {
    ("local_file", "read"): ((), ()),
    ("github", "repo_metadata"): ((), ()),
    ("gmail", "list_messages"): ((), ()),
    ("calendar", "list_events"): ((), ()),
    ("calendar", "create_event"): (("external_send",), ("summary", "description")),
    ("telegram", "send_message"): (("external_send",), ("text",)),
    ("telegram", "answer_callback_query"): (("external_send",), ("text",)),
    ("telegram", "edit_message_reply_markup"): (("external_send",), ()),
    ("stripe", "create_checkout_session"): (("payment",), ()),
    ("stripe", "expire_checkout_session"): (("payment", "destructive_action"), ()),
    ("stripe", "create_refund"): (("refund",), ()),
    ("stripe", "retrieve_checkout_session"): ((), ()),
    ("stripe", "retrieve_refund"): ((), ()),
}
_BROWSER_ACTIONS = {
    "BROWSER_OPEN",
    "BROWSER_SNAPSHOT",
    "BROWSER_CLICK",
    "BROWSER_TYPE",
    "BROWSER_SELECT",
    "BROWSER_SUBMIT",
    "BROWSER_SCREENSHOT",
    "BROWSER_SCROLL",
}
_RISK_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
_NEXT = {
    Decision.ALLOW: "execute",
    Decision.BLOCK: "blocked",
    Decision.SANITIZE: "sanitize",
    Decision.ASK_USER: "ask_user",
    Decision.NEED_APPROVAL: "approval_queue",
}
_HINTS = {
    "destructive": "destructive_action",
    "payment": "payment_related",
    "refund": "payment_related",
    "file_read": "",
}
_SECRET_ENTITY_KINDS = frozenset(
    {
        "AWS_ACCESS_KEY",
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "OPENAI_KEY",
        "STRIPE_KEY",
        "SLACK_TOKEN",
        "GOOGLE_API_KEY",
        "JWT",
        "PRIVATE_KEY",
        "CREDENTIAL_ASSIGNMENT",
        "GENERIC_SECRET",
        # Equivalent labels from the application rule engine.
        "api_key",
        "stripe_api_key",
        "stripe_webhook_secret",
        "aws_access_key",
        "github_token",
        "credential",
    }
)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _enforcement_decision(
    first: Decision,
    second: Decision,
    *,
    evaluation_error: bool = False,
) -> Decision:
    """Resolve two verdicts by enforcement prerequisite, not enum order.

    A block is terminal. Clarification precedes approval because an approval of
    an action whose intent is still unknown is not meaningful. Once intent is
    clear, approval dominates redaction so the router cannot execute the
    action until an explicit approval has occurred; the redacted replacement
    remains attached to the response and is substituted before execution.
    Detector errors are fail-closed at approval unless a block already exists.
    """
    decisions = {first, second}
    if Decision.BLOCK in decisions:
        return Decision.BLOCK
    if Decision.ASK_USER in decisions:
        return Decision.ASK_USER
    if evaluation_error:
        return Decision.NEED_APPROVAL
    if Decision.NEED_APPROVAL in decisions:
        return Decision.NEED_APPROVAL
    if Decision.SANITIZE in decisions:
        return Decision.SANITIZE
    return Decision.ALLOW


def _walk_redact(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {key: _walk_redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk_redact(item) for item in value]
    return value


def _detector_safe_value(value: Any, *, credential_input: bool = False) -> Any:
    """Remove known credential values before they become detector prompt text.

    PII remains visible to the PII detector. Known credential fields and
    patterns are already handled by the deterministic host rule engine, so
    redacting them before an Ollama request cannot weaken that floor.
    """
    if isinstance(value, dict):
        label = " ".join(
            str(value.get(key) or "") for key in ("label", "element_label", "name", "role")
        ).lower()
        is_credential_input = credential_input or any(
            token in label
            for token in ("password", "passwd", "secret", "token", "api key", "otp", "pin")
        )
        safe: dict[str, Any] = {}
        for key, item in value.items():
            should_redact = is_sensitive_key(str(key)) or (
                is_credential_input and str(key) in {"value", "text", "query"}
            )
            if should_redact and item not in (None, ""):
                safe[str(key)] = "[REDACTED]"
            else:
                safe[str(key)] = _detector_safe_value(item, credential_input=is_credential_input)
        return safe
    if isinstance(value, list):
        return [_detector_safe_value(item, credential_input=credential_input) for item in value]
    if isinstance(value, str):
        # The host rule engine deliberately owns the same credential patterns.
        from app.domains.guardrail.decision.simple import _redact_string

        return _redact_string(value)[0]
    return value


def _browser_prototype_redaction(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact content fields in validated compound browser actions only."""
    replacement = deepcopy(payload)

    def redact_action(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        copied = dict(item)
        if copied.get("type") in {"fill", "select"}:
            for key in ("value", "text", "query"):
                if key in copied:
                    copied[key] = _walk_redact(copied[key])
        return copied

    if isinstance(replacement.get("action"), dict):
        replacement["action"] = redact_action(replacement["action"])
    if isinstance(replacement.get("actions"), list):
        replacement["actions"] = [redact_action(item) for item in replacement["actions"]]
    return replacement


def _sanitized_payload(
    action: ActionRequest, content_fields: tuple[str, ...]
) -> dict[str, Any] | None:
    payload = deepcopy(action.payload)
    for key in content_fields:
        if key in payload:
            payload[key] = _walk_redact(payload[key])
    if action.action_type == "BROWSER_PROTOTYPE_ACTION":
        payload = _browser_prototype_redaction(payload)
    return payload if payload != action.payload else None


def prepare(action: ActionRequest) -> tuple[EngineRequest, DecisionResponse, tuple[str, ...]]:
    """Scan all arguments, while enforcing metadata from actual connector code."""
    settings = get_settings()
    normalized = build_action_request({**action.model_dump(), "payload": deepcopy(action.payload)})
    operation = str(action.payload.get("action") or "")
    browser = action.target_system == "browser"
    engine_type = action.action_type
    metadata = _TOOLS.get((action.target_system, operation))
    known = metadata is not None
    hints: tuple[str, ...]
    content_fields: tuple[str, ...]
    if browser:
        known = action.action_type in _BROWSER_ACTIONS
        hints = ("external_send",) if action.action_type == "BROWSER_SUBMIT" else ()
        content_fields = (
            ("value", "text", "query")
            if action.action_type in {"BROWSER_TYPE", "BROWSER_SELECT"}
            else ()
        )
        domain = action.domain
        if action.action_type == "BROWSER_PROTOTYPE_ACTION":
            actions = action.payload.get("actions") or (
                [action.payload["action"]] if action.payload.get("action") else []
            )
            supported = {"click", "fill", "select", "submit", "scroll", "screenshot"}
            known = isinstance(actions, list) and all(
                isinstance(item, dict) and item.get("type") in supported for item in actions
            )
            if known:
                types = {item["type"] for item in actions}
                engine_type = next(
                    (
                        kind
                        for raw, kind in (
                            ("submit", "BROWSER_SUBMIT"),
                            ("fill", "BROWSER_TYPE"),
                            ("click", "BROWSER_CLICK"),
                            ("select", "BROWSER_SELECT"),
                        )
                        if raw in types
                    ),
                    "BROWSER_OPEN",
                )
                if "submit" in types:
                    hints = ("external_send",)
                if any(
                    browser_action_needs_payment_approval(item["type"], item, action.user_goal)
                    for item in actions
                ):
                    hints = (*hints, "payment")
            # Only validated fill/select content fields are rewritten. Routing
            # metadata in the compound action list remains unchanged.
    else:
        hints, content_fields = metadata or ((), ())
        domain = settings.DOMAIN_BY_TARGET_SYSTEM.get(action.target_system, settings.DEFAULT_DOMAIN)
        expected_type = "FILE_READ" if action.target_system == "local_file" else "API_CALL"
        known = known and action.action_type == expected_type
    normalized = normalized.model_copy(update={"domain": domain})
    rule = decide_rule(normalized)
    # Preserve missing-field clarification; otherwise trusted capabilities set
    # a minimum verdict even if the caller claims a harmless domain/hint.
    if rule.decision != Decision.ASK_USER:
        for hint in sorted({*hints, normalized.risk_hint}):
            floor = decide_rule(normalized.model_copy(update={"risk_hint": hint, "payload": {}}))
            strongest = _enforcement_decision(rule.decision, floor.decision)
            rule = rule.model_copy(
                update={
                    "decision": strongest,
                    "next_step": _NEXT[strongest],
                    "risk_score": max(rule.risk_score, floor.risk_score),
                    "risk_level": max(
                        (rule.risk_level, floor.risk_level), key=_RISK_RANK.__getitem__
                    ),
                    "reasons": _dedupe([*rule.reasons, *floor.reasons]),
                    "triggered_policies": _dedupe(
                        [*rule.triggered_policies, *floor.triggered_policies]
                    ),
                }
            )
    if not known:
        rule = rule.model_copy(
            update={
                "decision": Decision.BLOCK,
                "risk_level": RiskLevel.CRITICAL,
                "risk_score": 1.0,
                "reasons": _dedupe(
                    [*rule.reasons, "Unregistered connector operation or incompatible action type"]
                ),
                "triggered_policies": _dedupe(
                    [*rule.triggered_policies, "host.registered_tool_required"]
                ),
                "next_step": "blocked",
            }
        )
    elif _SECRET_ENTITY_KINDS.intersection(rule.sensitive_entities) and engine_type in {
        "API_CALL",
        "BROWSER_SUBMIT",
        "BROWSER_TYPE",
    }:
        # Detector prompts receive a redacted copy of known credentials. Keep
        # the upstream secret-egress guarantee at the host boundary as well.
        rule = rule.model_copy(
            update={
                "decision": Decision.BLOCK,
                "risk_level": RiskLevel.CRITICAL,
                "risk_score": max(rule.risk_score, 0.95),
                "reasons": _dedupe(
                    [*rule.reasons, "Secret or credential material cannot be sent externally"]
                ),
                "triggered_policies": _dedupe([*rule.triggered_policies, "code.secret_egress"]),
                "next_step": "blocked",
            }
        )
    effective_hints = {normalized.risk_hint, *hints}
    if action.browser_element is not None:
        effective_hints.add(action.browser_element.risk_hint)
    engine_hints = sorted({_HINTS.get(hint, hint) for hint in effective_hints} - {"", "unknown"})
    context = action.content_context
    if action.browser_element is not None:
        context += "\n" + action.browser_element.model_dump_json()
    safe_target = _detector_safe_value(action.target)
    request = EngineRequest(
        action_type=engine_type,
        domain="booking_style" if domain == "booking" else domain,
        target_system={
            "calendar": "Google Calendar",
            "gmail": "Gmail",
            "github": "GitHub",
            "local_file": "Local Filesystem",
            "telegram": "Telegram",
            "stripe": "Stripe",
            "browser": "Browser",
        }.get(action.target_system, action.target_system),
        tool_name=action.action_type if browser else f"{action.target_system}.{operation}",
        target=safe_target if isinstance(safe_target, str) else json.dumps(safe_target),
        payload_summary=_detector_safe_value(action.payload_summary),
        content_context=_detector_safe_value(context),
        raw_payload=json.dumps(_detector_safe_value(action.payload), ensure_ascii=False),
        risk_hint=engine_hints,
        rollback_available=(
            not bool(hints)
            if not browser
            else action.action_type
            in {
                "BROWSER_OPEN",
                "BROWSER_SNAPSHOT",
                "BROWSER_SCREENSHOT",
                "BROWSER_SCROLL",
            }
        ),
        confidence=action.confidence,
    )
    return request, rule, content_fields


def _translate(
    action: ActionRequest,
    upstream: EngineResponse,
    rule: DecisionResponse,
    content_fields: tuple[str, ...],
) -> DecisionResponse:
    decision = _enforcement_decision(
        Decision(upstream.decision.value),
        rule.decision,
        evaluation_error=bool(upstream.evaluation_error),
    )
    entities = sorted(
        {entity.kind for entity in upstream.sensitive_entities} | set(rule.sensitive_entities)
    )
    sanitized: dict[str, Any] | None = None
    if upstream.sanitized_payload is not None or rule.sanitized_payload is not None:
        sanitized = _sanitized_payload(action, content_fields)

    reasons = _dedupe([*rule.reasons, *upstream.reasons])
    policies = _dedupe([*rule.triggered_policies, *upstream.triggered_policies])
    risk_level = max(
        (RiskLevel(upstream.risk_level.value), rule.risk_level), key=_RISK_RANK.__getitem__
    )
    risk_score = max(upstream.risk_score, rule.risk_score)

    # A secret that cannot be rewritten into a supported content field must
    # not reach an executor through a later approval. PII routing metadata is
    # intentionally handled by the normal approval flow instead.
    if _SECRET_ENTITY_KINDS.intersection(entities) and sanitized is None:
        decision = Decision.BLOCK
        risk_level = RiskLevel.CRITICAL
        risk_score = max(risk_score, 0.95)
        reasons = _dedupe(
            [*reasons, "Sensitive credential material cannot be safely redacted for this action"]
        )
        policies = _dedupe([*policies, "host.unredactable_sensitive_content"])
    if decision == Decision.SANITIZE and sanitized is None:
        decision = Decision.NEED_APPROVAL
    return DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=decision,
        risk_level=risk_level,
        risk_score=risk_score,
        reasons=reasons,
        triggered_policies=policies,
        sensitive_entities=entities,
        sanitized_payload=sanitized,
        next_step=_NEXT[decision],
        evaluation_error=upstream.evaluation_error,
    )


class EvaluationAudit:
    """Persist the final mapped verdict before DecisionEngine returns it.

    This journal is separate from the existing write-once final action audit.
    A repeated evaluation (after input/redaction) gets a new ID, retaining the
    same run/action IDs. It works without PostgreSQL in the local CLI.
    """

    def __init__(
        self, action: ActionRequest, rule: DecisionResponse, content_fields: tuple[str, ...]
    ) -> None:
        self.action, self.rule, self.content_fields = action, rule, content_fields
        self.response: DecisionResponse | None = None

    def record(self, request: EngineRequest, response: EngineResponse, stage: str) -> str:
        # Import lazily: the host sanitizer itself uses the legacy redactor.
        from app.runtime.safety import Sanitizer

        mapped = _translate(self.action, response, self.rule, self.content_fields)
        audit_id = new_id("guard")
        mapped.guardrail_audit_id = audit_id
        response.audit_id = audit_id
        record = {
            "schema_version": "0.1",
            "audit_id": audit_id,
            "stage": stage,
            "run_id": self.action.run_id,
            "action_id": self.action.action_id,
            "created_at": mapped.created_at.isoformat(),
            # Keep structured values for key-aware secret masking. Do not persist
            # the serialized raw_payload, which loses that key information.
            "request": {**self.action.model_dump(mode="json"), "payload": self.action.payload},
            "tool_name": request.tool_name,
            "decision": mapped.model_dump(mode="json"),
        }
        runtime = current_runtime()
        clean = (
            runtime.sanitize_messages
            if runtime and runtime.sanitize_messages
            else Sanitizer().clean
        )
        safe = _walk_redact(clean(record))
        path = Path(get_settings().GUARDRAIL_AUDIT_PATH)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink():
                raise OSError("Audit path must not be a symlink")
            with FileLock(str(path) + ".lock", timeout=10, mode=0o600):
                descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(safe, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        except (OSError, TimeoutError, FileLockTimeout) as exc:
            raise AuditUnavailable(
                "Guardrail audit could not be persisted; action not executed"
            ) from exc
        self.response = mapped
        return audit_id


def decide_agentgate(action: ActionRequest) -> DecisionResponse:
    started = perf_counter()
    settings = get_settings()
    request, rule, content_fields = prepare(action)
    options: dict[str, Any] = {
        "model": settings.AGENTGATE_LLM_DETECTOR_MODEL,
        "host": settings.OLLAMA_HOST,
        "timeout": settings.AGENTGATE_LLM_DETECTOR_TIMEOUT,
    }
    classes = (
        [LLMUnifiedDetector]
        if settings.AGENTGATE_DETECTOR_ARCHITECTURE == "unified"
        else [
            LLMPIIDetector,
            LLMSecretDetector,
            LLMSourceCodeDetector,
            LLMPaymentPhishingDetector,
            LLMPromptInjectionDetector,
            LLMActionIntentDetector,
        ]
    )
    detectors = [] if rule.decision == Decision.BLOCK else [cls(**options) for cls in classes]
    audit = EvaluationAudit(action, rule, content_fields)
    DecisionEngine(detectors=detectors, audit_store=audit).evaluate(request)
    assert audit.response is not None
    audit.response.latency_ms = int((perf_counter() - started) * 1000)
    return audit.response


async def adecide_agentgate(action: ActionRequest) -> DecisionResponse:
    # The upstream engine uses synchronous HTTP and a detector thread pool.
    # Keep that work off the API/CLI event loop; to_thread propagates runtime context.
    return await asyncio.to_thread(decide_agentgate, action.model_copy(deep=True))
