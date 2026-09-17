"""Full-LLM detector classes for every production detection category."""

from __future__ import annotations

from typing import Any

from ..schemas import ActionRequest, SensitiveEntity
from . import llm_client
from .base import Detector, Finding, truncate
from .llm_client import LLMUnavailable
from .llm_validation import (
    require_bool,
    require_confidence,
    require_items,
    require_nonnegative_int,
    require_string,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _call_llm(system_prompt: str, text: str, *, model: str | None = None,
               host: str | None = None, timeout: float | None = None,
               extra_options: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Call the required detector runtime; empty action text needs no model call."""
    if not text:
        return None
    return llm_client.chat_json(
        system_prompt,
        f"TEXT: {text}",
        model=model,
        host=host,
        timeout=timeout,
        extra_options=extra_options,
    )


class _LLMDetectorBase(Detector):
    """Common init shared by every full-LLM detector."""

    def __init__(self, model: str | None = None, host: str | None = None,
                 timeout: float | None = None,
                 extra_options: dict[str, Any] | None = None):
        self.model = model
        self.host = host
        self.timeout = timeout
        self.extra_options = extra_options

    def _llm(self, system_prompt: str, text: str) -> dict | None:
        return _call_llm(system_prompt, text, model=self.model, host=self.host,
                         timeout=self.timeout, extra_options=self.extra_options)


# ---------------------------------------------------------------------------
# 1. PII Detector (LLM)
# ---------------------------------------------------------------------------

_PII_PROMPT = (
    "You are a PII (Personally Identifiable Information) classifier inside an "
    "AI-agent guardrail. Analyse the TEXT and identify any PII present: email "
    "addresses, phone numbers, credit card numbers, booking/order references. "
    "Reply ONLY as JSON: "
    '{"has_pii": true|false, "items": [{"type": "EMAIL"|"PHONE"|"CREDIT_CARD"|"BOOKING_REF", '
    '"value": "<the detected value>", "severity": "LOW"|"MEDIUM"|"HIGH"}]}'
)


class LLMPIIDetector(_LLMDetectorBase):
    name = "pii"

    def scan(self, req: ActionRequest) -> Finding:
        data = self._llm(_PII_PROMPT, req.scan_text)
        if data is None:
            return self._finding()
        has_pii = require_bool(data, "has_pii")
        if not has_pii:
            # "items" is legitimately absent on a well-formed negative response -
            # only require it (and validate its shape) when has_pii actually says
            # there's something to list. Requiring it unconditionally here was
            # raising LLMUnavailable on correct, common "nothing found" responses.
            if data.get("items"):
                raise LLMUnavailable("PII response contradicts has_pii=false")
            return self._finding()
        items = require_items(data)
        if not items:
            raise LLMUnavailable("PII response contradicts has_pii=true")

        entities: list[SensitiveEntity] = []
        reasons: list[str] = []
        sev_weight = {"LOW": 0.1, "MEDIUM": 0.25, "HIGH": 0.45, "CRITICAL": 0.6}

        allowed_kinds = {"EMAIL", "PHONE", "CREDIT_CARD", "BOOKING_REF"}
        for item in items:
            kind = require_string(item, "type", allowed_kinds)
            require_string(item, "value")
            severity = require_string(item, "severity", {"LOW", "MEDIUM", "HIGH"})
            entities.append(
                SensitiveEntity(kind, f"[REDACTED_{kind}]", self.name, severity)
            )

        if entities:
            kinds = sorted({e.kind for e in entities})
            reasons.append(f"PII / customer data detected: {', '.join(kinds)}")

        contribution = min(0.6, sum(sev_weight.get(e.severity, 0.25) for e in entities))
        return self._finding(entities=entities, reasons=reasons, risk_contribution=contribution)


# ---------------------------------------------------------------------------
# 2. Secret Detector (LLM)
# ---------------------------------------------------------------------------

_SECRET_PROMPT = (
    "You are a secret/credential classifier inside an AI-agent guardrail. "
    "Analyse the TEXT and identify any secrets, API keys, tokens, passwords, "
    "private keys, or credential assignments. "
    "Only report a finding when an actual credential VALUE is present in the "
    "text, a real-looking key/token/password string. Do NOT report a finding "
    "just because the text mentions a service name (e.g. 'stripe', 'aws', "
    "'github'), contains a payment or checkout LINK/URL, or calls a library/SDK "
    "by name (e.g. 'import stripe', 'stripe.Charge.create') without an actual "
    "secret value being present - a payment link is not a credential. "
    "Opaque alphanumeric or hex identifiers with no surrounding credential "
    "context - message IDs, UUIDs, object IDs, database row keys - are NOT "
    "credentials on their own, no matter how random they look. Only report one "
    "of these as a GENERIC_SECRET or a specific provider type if it also "
    "matches a recognizable credential FORMAT: a known provider prefix (AKIA, "
    "ghp_, github_pat_, sk-, xox, AIza), a PEM/PGP private key block, or a "
    "three-part JWT (header.payload.signature). "
    "The ENV_FILE type is different: report it whenever the text describes "
    "reading, accessing, or targeting a file whose PATH or NAME is a "
    "conventionally sensitive credentials file - .env, .pem, .key, "
    "id_rsa, credentials.json, secrets.yaml, and similar - even if no literal "
    "secret VALUE is shown yet, because the file itself is the signal; you do "
    "not need to see its contents to flag that access to it is happening. "
    "An explicit NAME=value assignment where NAME looks like a secret "
    "(API_KEY, PASSWORD, TOKEN, SECRET) should be reported even if the value "
    "looks like an opaque placeholder. "
    "Reply ONLY as JSON: "
    '{"has_secrets": true|false, "items": [{"type": "AWS_ACCESS_KEY"|"GITHUB_TOKEN"|'
    '"GITHUB_PAT"|"OPENAI_KEY"|"SLACK_TOKEN"|"STRIPE_KEY"|"GOOGLE_API_KEY"|'
    '"PRIVATE_KEY"|"CREDENTIAL_ASSIGNMENT"|"JWT"|"ENV_FILE"|"GENERIC_SECRET", '
    '"value": "<masked preview>", "severity": "HIGH"|"CRITICAL"}]}'
)


class LLMSecretDetector(_LLMDetectorBase):
    name = "secret"

    def scan(self, req: ActionRequest) -> Finding:
        data = self._llm(_SECRET_PROMPT, req.scan_text)
        if data is None:
            return self._finding()
        has_secrets = require_bool(data, "has_secrets")
        if not has_secrets:
            # Same reasoning as the PII detector above: "items" is legitimately
            # absent on a well-formed negative response.
            if data.get("items"):
                raise LLMUnavailable("secret response contradicts has_secrets=false")
            return self._finding()
        items = require_items(data)
        if not items:
            raise LLMUnavailable("secret response contradicts has_secrets=true")

        entities: list[SensitiveEntity] = []
        reasons: list[str] = []
        tags: set[str] = set()

        allowed_kinds = {
            "AWS_ACCESS_KEY",
            "GITHUB_TOKEN",
            "GITHUB_PAT",
            "OPENAI_KEY",
            "SLACK_TOKEN",
            "STRIPE_KEY",
            "GOOGLE_API_KEY",
            "PRIVATE_KEY",
            "CREDENTIAL_ASSIGNMENT",
            "JWT",
            "ENV_FILE",
            "GENERIC_SECRET",
        }
        for item in items:
            kind = require_string(item, "type", allowed_kinds)
            require_string(item, "value")
            severity = require_string(item, "severity", {"HIGH", "CRITICAL"})
            entities.append(
                SensitiveEntity(kind, f"[REDACTED_{kind}]", self.name, severity)
            )

        if entities:
            tags.add("source_code")
            kinds = sorted({e.kind for e in entities})
            reasons.append(f"Secret/credential material detected: {', '.join(kinds)}")

        has_critical = any(e.severity == "CRITICAL" for e in entities)
        contribution = 0.85 if has_critical else 0.55 if entities else 0.0
        return self._finding(entities=entities, reasons=reasons,
                             risk_contribution=contribution, tags=tags)


# ---------------------------------------------------------------------------
# 3. Source Code Detector (LLM)
# ---------------------------------------------------------------------------

_SOURCE_CODE_PROMPT = (
    "You are a source-code classifier inside an AI-agent guardrail. Analyse the "
    "TEXT and determine whether it contains programming source code or internal "
    "codenames/project names. "
    "Reply ONLY as JSON: "
    '{"has_code": true|false, "has_codename": true|false, '
    '"language": "<detected language or empty>", "confidence": 0.0-1.0}'
)


class LLMSourceCodeDetector(_LLMDetectorBase):
    name = "source_code"

    def scan(self, req: ActionRequest) -> Finding:
        data = self._llm(_SOURCE_CODE_PROMPT, req.scan_text)
        if data is None:
            if "source_code" in req.risk_hint:
                return self._finding(
                    entities=[
                        SensitiveEntity(
                            "SOURCE_CODE",
                            "[TRUSTED_SOURCE_CODE_METADATA]",
                            self.name,
                            "MEDIUM",
                        )
                    ],
                    reasons=["Trusted tool metadata identifies source-code content"],
                    risk_contribution=0.3,
                    tags={"source_code"},
                )
            return self._finding()

        has_code = require_bool(data, "has_code")
        has_codename = require_bool(data, "has_codename")
        language = require_string(data, "language")
        confidence = require_confidence(data)
        if not has_code and not has_codename and language:
            raise LLMUnavailable("source-code response contradicts has_code=false")

        entities: list[SensitiveEntity] = []
        reasons: list[str] = []
        tags: set[str] = set()
        contribution = 0.0

        if has_code or "source_code" in req.risk_hint:
            tags.add("source_code")
            language = language or "unknown"
            entities.append(SensitiveEntity("SOURCE_CODE",
                            truncate(f"{language} code (conf={confidence:.2f})"),
                            self.name, "MEDIUM"))
            reasons.append(
                f"Source code detected ({language}, confidence {confidence:.2f})"
                if has_code
                else "Trusted tool metadata identifies source-code content"
            )
            contribution = 0.3
            if "external_send" in req.risk_hint or req.action_type == "BROWSER_SUBMIT":
                contribution = 0.6
                reasons.append("Source code paired with an outbound/send action")

        if has_codename:
            tags.add("source_code")
            entities.append(
                SensitiveEntity(
                    "INTERNAL_CODENAME",
                    "[REDACTED_INTERNAL_CODENAME]",
                    self.name,
                    "MEDIUM",
                )
            )
            reasons.append("Internal codename detected")
            contribution = max(contribution, 0.25)

        return self._finding(entities=entities, reasons=reasons,
                             risk_contribution=contribution, tags=tags)


# ---------------------------------------------------------------------------
# 4. Payment / Phishing Detector (LLM)
# ---------------------------------------------------------------------------

_PAYMENT_PROMPT = (
    "You are a payment/phishing classifier inside an AI-agent guardrail. "
    "Analyse the TEXT and determine whether it contains payment-related content "
    "(invoices, refunds, charges, payment links), credential requests (asking "
    "for passwords/PINs/OTPs), or urgency/phishing patterns. "
    "Reply ONLY as JSON: "
    '{"has_payment": true|false, "has_credential_request": true|false, '
    '"has_urgency": true|false, "confidence": 0.0-1.0}'
)


class LLMPaymentPhishingDetector(_LLMDetectorBase):
    name = "payment_phishing"

    def scan(self, req: ActionRequest) -> Finding:
        data = self._llm(_PAYMENT_PROMPT, req.scan_text)
        if data is None:
            if "payment_related" in req.risk_hint:
                external = "external_send" in req.risk_hint
                return self._finding(
                    entities=[
                        SensitiveEntity(
                            "PAYMENT_CONTENT",
                            "[TRUSTED_PAYMENT_METADATA]",
                            self.name,
                            "HIGH",
                        )
                    ],
                    reasons=[
                        "Trusted tool metadata identifies payment-related content",
                        *(
                            ["Payment content is paired with an external send"]
                            if external
                            else []
                        ),
                    ],
                    risk_contribution=0.6 if external else 0.5,
                    tags={"payment_related", *({"external_send"} if external else set())},
                )
            return self._finding()
        model_payment = require_bool(data, "has_payment")
        has_cred = require_bool(data, "has_credential_request")
        has_urgency = require_bool(data, "has_urgency")
        require_confidence(data)

        entities: list[SensitiveEntity] = []
        reasons: list[str] = []
        tags: set[str] = set()
        contribution = 0.0

        has_payment = model_payment or "payment_related" in req.risk_hint

        if has_payment:
            tags.add("payment_related")
            entities.append(SensitiveEntity("PAYMENT_CONTENT",
                            truncate(req.scan_text[:48]), self.name, "HIGH"))
            reasons.append("Payment-related content detected")
            contribution = 0.5

        if has_cred:
            entities.append(SensitiveEntity("CREDENTIAL_REQUEST",
                            truncate(req.scan_text[:48]), self.name, "CRITICAL"))
            reasons.append("Message requests credentials (phishing pattern)")
            contribution = max(contribution, 0.8)

        if has_urgency and (has_payment or has_cred):
            reasons.append("Urgency + payment/credential pattern (phishing-like)")
            contribution = min(0.9, contribution + 0.2)

        if contribution and ("external_send" in req.risk_hint or req.action_type == "BROWSER_SUBMIT"):
            tags.add("external_send")
            contribution = min(0.9, contribution + 0.1)
            reasons.append("Payment/phishing content paired with external send")

        return self._finding(entities=entities, reasons=reasons,
                             risk_contribution=contribution, tags=tags)


# ---------------------------------------------------------------------------
# 5. Action Intent Detector (LLM)
# ---------------------------------------------------------------------------

_INTENT_PROMPT = (
    "You are an action-intent classifier inside an AI-agent guardrail. Analyse "
    "the TEXT and determine whether it expresses: (1) a bulk operation affecting "
    "many items, (2) a destructive action (delete/remove/purge/cancel), or "
    "(3) an outbound send to an external recipient. "
    "A bulk operation means a WRITE, DELETE, SEND, MODIFY, or ARCHIVE affecting "
    "many items at once. A read-only SEARCH, QUERY, FILTER, or LIST that merely "
    "looks through many items without changing any of them is NOT a bulk "
    "operation - answer is_bulk=false for those regardless of how many items are "
    "searched, since nothing is actually being modified. Similarly, a plain "
    "UPDATE to a single record's own fields (not a bulk update, not a send) is "
    "not bulk or destructive. "
    "is_external_send=true means a message or content is actually being "
    "transmitted now. Composing, drafting, previewing, or typing a message that "
    "has not yet been sent is NOT an external send, even when its eventual "
    "purpose is to go to an external recipient - answer is_external_send=false "
    "for those. Look for explicit not-yet-sent language ('draft', 'compose', "
    "'not sent', 'not submitted', 'preview') as the signal that nothing has left "
    "the system yet; answer is_external_send=true only once the text itself "
    "describes the send/submit/publish/forward actually happening. "
    "Reply ONLY as JSON: "
    '{"is_bulk": true|false, "estimated_count": <number or 0>, '
    '"is_destructive": true|false, "is_external_send": true|false, '
    '"confidence": 0.0-1.0}'
)


class LLMActionIntentDetector(_LLMDetectorBase):
    name = "action_intent"

    def scan(self, req: ActionRequest) -> Finding:
        data = self._llm(_INTENT_PROMPT, req.scan_text)
        if data is None:
            return self._finding()
        is_bulk = require_bool(data, "is_bulk")
        estimated_count = require_nonnegative_int(data, "estimated_count")
        is_destructive = require_bool(data, "is_destructive")
        is_external_send = require_bool(data, "is_external_send")
        require_confidence(data)
        if not is_bulk and estimated_count >= 20:
            raise LLMUnavailable("action-intent response contradicts is_bulk=false")

        reasons: list[str] = []
        tags: set[str] = set()
        contribution = 0.0

        if is_bulk:
            tags.add("bulk_action")
            reasons.append(f"Bulk operation detected ({estimated_count or 'many'} items)")
            contribution = max(contribution, 0.5)

        if is_destructive:
            tags.add("destructive_action")
            base = 0.7 if not req.rollback_available else 0.5
            reasons.append("Destructive verb detected (delete/cancel/purge/...)")
            contribution = max(contribution, base)

        if is_external_send:
            tags.add("external_send")
            reasons.append("Outbound send to an external recipient detected")
            contribution = max(contribution, 0.35)

        return self._finding(reasons=reasons, risk_contribution=contribution, tags=tags)
