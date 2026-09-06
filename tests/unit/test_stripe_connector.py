import asyncio
from types import SimpleNamespace

from app.config.settings import Settings
from app.core.action_request import build_action_request
from app.domains.guardrail.decision.simple import decide_rule
from app.domains.connector.stripe.stripe import StripeConnector
from app.domains.agent.services.guarded_execution import _safe_trace_tool_call
from app.llm.services.parser import _normalize_step


class FakePaymentStore:
    def __init__(self) -> None:
        self.sessions: list[dict] = []

    async def record_checkout_session(self, session_data: dict) -> None:
        self.sessions.append(session_data)

    async def process_event(self, event: dict) -> bool:
        return True


class FakeSessions:
    def __init__(self) -> None:
        self.created: list[tuple[dict, dict]] = []
        self.retrieved: list[str] = []
        self.expired: list[tuple[str, dict]] = []

    async def create_async(self, params: dict, options: dict):
        self.created.append((params, options))
        return SimpleNamespace(
            id="cs_test_123",
            url="https://checkout.stripe.com/c/pay/cs_test_123",
            status="open",
            payment_status="unpaid",
            payment_intent=None,
            amount_total=2500,
            currency="usd",
            expires_at=123456,
        )

    async def retrieve_async(self, session_id: str):
        self.retrieved.append(session_id)
        return SimpleNamespace(
            id=session_id,
            url=None,
            status="complete",
            payment_status="paid",
            payment_intent="pi_123",
            amount_total=2500,
            currency="usd",
            expires_at=123456,
        )

    async def expire_async(self, session_id: str, options: dict):
        self.expired.append((session_id, options))
        return SimpleNamespace(
            id=session_id,
            url=None,
            status="expired",
            payment_status="unpaid",
            payment_intent=None,
            amount_total=2500,
            currency="usd",
            expires_at=123456,
        )


class FakeRefunds:
    def __init__(self) -> None:
        self.created: list[tuple[dict, dict]] = []

    async def create_async(self, params: dict, options: dict):
        self.created.append((params, options))
        return SimpleNamespace(
            id="re_123",
            payment_intent=params["payment_intent"],
            status="succeeded",
            amount=params.get("amount", 2500),
            currency="usd",
            reason=params.get("reason"),
        )

    async def retrieve_async(self, refund_id: str):
        return SimpleNamespace(
            id=refund_id,
            payment_intent="pi_123",
            status="succeeded",
            amount=2500,
            currency="usd",
            reason="requested_by_customer",
        )


class FakeStripeClient:
    def __init__(self) -> None:
        self.sessions = FakeSessions()
        self.refunds = FakeRefunds()
        self.v1 = SimpleNamespace(
            checkout=SimpleNamespace(sessions=self.sessions),
            refunds=self.refunds,
        )


def stripe_settings(**overrides) -> Settings:
    values = {
        "STRIPE_SECRET_KEY": "sk_test_example",
        "STRIPE_WEBHOOK_SECRET": "whsec_example",
        "STRIPE_PRICE_MAP": {"workshop_ticket": "price_123"},
        "STRIPE_SUCCESS_URL": "https://agentgate.example/payment/success",
        "STRIPE_CANCEL_URL": "https://agentgate.example/payment/cancel",
        "STRIPE_MAX_QUANTITY": 5,
    }
    values.update(overrides)
    return Settings(**values)


def test_create_checkout_uses_server_catalog_urls_and_idempotency_key():
    client = FakeStripeClient()
    store = FakePaymentStore()
    connector = StripeConnector(
        client,
        settings_factory=stripe_settings,
        payment_store=store,
    )

    result = asyncio.run(
        connector.execute(
            "create_checkout_session",
            {
                "run_id": "run_1",
                "action_id": "act_1",
                "catalog_key": "workshop_ticket",
                "quantity": 2,
                "customer_email": "buyer@example.com",
                "price_id": "price_attacker_supplied",
                "success_url": "https://attacker.example/success",
            },
        )
    )

    assert result.status == "SUCCESS"
    assert result.data == {
        "id": "cs_test_123",
        "url": "https://checkout.stripe.com/c/pay/cs_test_123",
        "status": "open",
        "payment_status": "unpaid",
        "payment_intent": None,
        "amount_total": 2500,
        "currency": "usd",
        "expires_at": 123456,
    }
    params, options = client.sessions.created[0]
    assert params["line_items"] == [{"price": "price_123", "quantity": 2}]
    assert params["success_url"] == "https://agentgate.example/payment/success"
    assert params["cancel_url"] == "https://agentgate.example/payment/cancel"
    assert params["metadata"] == {
        "run_id": "run_1",
        "action_id": "act_1",
        "catalog_key": "workshop_ticket",
    }
    assert options == {"idempotency_key": "agentgate:run_1:act_1:create_checkout_session"}
    assert store.sessions[0]["id"] == "cs_test_123"
    assert "customer_email" not in store.sessions[0]


def test_create_checkout_rejects_unknown_catalog_key_before_calling_stripe():
    client = FakeStripeClient()
    connector = StripeConnector(
        client,
        settings_factory=stripe_settings,
        payment_store=FakePaymentStore(),
    )

    result = asyncio.run(
        connector.execute(
            "create_checkout_session",
            {"run_id": "run_1", "action_id": "act_1", "catalog_key": "invented"},
        )
    )

    assert result.status == "FAILED"
    assert result.error["code"] == "VALIDATION"
    assert client.sessions.created == []


def test_create_checkout_rejects_non_https_redirects_outside_localhost():
    client = FakeStripeClient()
    connector = StripeConnector(
        client,
        settings_factory=lambda: stripe_settings(STRIPE_SUCCESS_URL="http://example.com/success"),
        payment_store=FakePaymentStore(),
    )

    result = asyncio.run(
        connector.execute(
            "create_checkout_session",
            {
                "run_id": "run_1",
                "action_id": "act_1",
                "catalog_key": "workshop_ticket",
            },
        )
    )

    assert result.status == "FAILED"
    assert "HTTPS" in result.result_summary
    assert client.sessions.created == []


def test_create_refund_validates_and_uses_idempotency_key():
    client = FakeStripeClient()
    connector = StripeConnector(
        client,
        settings_factory=stripe_settings,
        payment_store=FakePaymentStore(),
    )

    result = asyncio.run(
        connector.execute(
            "create_refund",
            {
                "run_id": "run_2",
                "action_id": "act_2",
                "payment_intent_id": "pi_123",
                "amount": 1200,
                "reason": "requested_by_customer",
            },
        )
    )

    assert result.status == "SUCCESS"
    params, options = client.refunds.created[0]
    assert params["payment_intent"] == "pi_123"
    assert params["amount"] == 1200
    assert params["metadata"] == {"run_id": "run_2", "action_id": "act_2"}
    assert options == {"idempotency_key": "agentgate:run_2:act_2:refund"}


def test_action_request_forces_stripe_financial_policy_and_safe_summary():
    action = build_action_request(
        {
            "action_type": "API_CALL",
            "target_system": "stripe",
            "target": "price",
            "domain": "browser",
            "risk_hint": "unknown",
            "payload": {
                "action": "create_checkout_session",
                "catalog_key": "workshop_ticket",
                "quantity": 2,
                "customer_email": "buyer@example.com",
            },
        }
    )

    assert action.domain == "booking"
    assert action.risk_hint == "payment"
    assert action.payload_summary == "create checkout for workshop_ticket, quantity 2"
    assert "buyer@example.com" not in action.payload_summary
    assert decide_rule(action).decision == "NEED_APPROVAL"


def test_parser_normalization_forces_stripe_financial_policy():
    step = _normalize_step(
        {
            "action_type": "API_CALL",
            "target_system": "stripe",
            "domain": "browser",
            "risk_hint": "unknown",
            "payload": {
                "action": "create_refund",
                "payment_intent_id": "pi_123",
            },
        }
    )

    assert step is not None
    assert step["domain"] == "booking"
    assert step["risk_hint"] == "refund"
    assert step["target"] == "pi_123"


def test_stripe_customer_email_is_removed_from_persistent_trace():
    proposal = {
        "target_system": "stripe",
        "payload": {
            "action": "create_checkout_session",
            "catalog_key": "workshop_ticket",
            "customer_email": "buyer@example.com",
        },
    }

    safe = _safe_trace_tool_call(proposal)

    assert "customer_email" not in safe["payload"]
    assert proposal["payload"]["customer_email"] == "buyer@example.com"


def test_guardrail_sanitizes_stripe_api_and_webhook_secrets():
    action = build_action_request(
        {
            "action_type": "API_CALL",
            "target_system": "telegram",
            "target": "recipient",
            "payload": {
                "action": "send_message",
                "text": (
                    "keys sk_test_abcdefghijklmnopqrstuvwxyz "
                    "and whsec_abcdefghijklmnopqrstuvwxyz"
                ),
            },
        }
    )

    decision = decide_rule(action)

    assert decision.decision == "SANITIZE"
    assert decision.sensitive_entities == ["stripe_api_key", "stripe_webhook_secret"]
    assert decision.sanitized_payload is not None
    assert decision.sanitized_payload["text"] == "keys [REDACTED] and [REDACTED]"
