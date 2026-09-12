"""Guarded Stripe connector with a deliberately small financial surface."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import stripe

from app.config.settings import Settings, get_settings
from app.core.errors import ConnectorError, ConnectorErrorCode
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector
from app.domains.connector.stripe.repository import StripePaymentRepository, StripePaymentStore

logger = logging.getLogger(__name__)

SUPPORTED_STRIPE_ACTIONS = {
    "create_checkout_session",
    "retrieve_checkout_session",
    "expire_checkout_session",
    "create_refund",
    "retrieve_refund",
}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class StripeConnector(BaseConnector):
    """Execute an allowlisted subset of Stripe operations.

    Checkout prices and redirect URLs come exclusively from server settings.
    The planner can select a catalog key and quantity, but cannot manufacture
    arbitrary prices, currencies, or callback destinations.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        settings_factory: Callable[[], Settings] = get_settings,
        payment_store: StripePaymentStore | None = None,
    ) -> None:
        self._client = client
        self._settings_factory = settings_factory
        self._payments = payment_store or StripePaymentRepository()

    async def execute(self, action: str, payload: dict[str, Any]) -> ExecutionResult:
        started = perf_counter()
        run_id = _text(payload.get("run_id"))
        action_id = _text(payload.get("action_id"))

        if action not in SUPPORTED_STRIPE_ACTIONS:
            return failed(run_id, action_id, "unsupported Stripe action", started=started)

        settings = self._settings_factory()
        client = self._client
        owned_http_client: Any | None = None
        if client is None:
            if not settings.STRIPE_SECRET_KEY:
                return failed(
                    run_id,
                    action_id,
                    "Stripe secret key is not configured",
                    ConnectorErrorCode.AUTH,
                    started=started,
                )
            owned_http_client = stripe.HTTPXClient()
            client = stripe.StripeClient(
                settings.STRIPE_SECRET_KEY,
                http_client=owned_http_client,
                max_network_retries=2,
            )

        try:
            if action == "create_checkout_session":
                return await self._create_checkout_session(
                    client, payload, settings, run_id, action_id, started
                )
            if action == "retrieve_checkout_session":
                return await self._retrieve_checkout_session(
                    client, payload, run_id, action_id, started
                )
            if action == "expire_checkout_session":
                return await self._expire_checkout_session(
                    client, payload, run_id, action_id, started
                )
            if action == "create_refund":
                return await self._create_refund(client, payload, run_id, action_id, started)
            if action == "retrieve_refund":
                return await self._retrieve_refund(client, payload, run_id, action_id, started)
        except stripe.AuthenticationError:
            return failed(
                run_id,
                action_id,
                "Stripe authentication failed",
                ConnectorErrorCode.AUTH,
                started=started,
            )
        except stripe.PermissionError:
            return failed(
                run_id,
                action_id,
                "Stripe permission denied",
                ConnectorErrorCode.PERMISSION,
                started=started,
            )
        except stripe.RateLimitError:
            return failed(
                run_id,
                action_id,
                "Stripe rate limit exceeded",
                ConnectorErrorCode.RATE_LIMIT,
                retryable=True,
                started=started,
            )
        except (stripe.InvalidRequestError, stripe.CardError) as exc:
            return failed(
                run_id,
                action_id,
                "Stripe rejected the request",
                ConnectorErrorCode.VALIDATION,
                details={"parameter": getattr(exc, "param", None)},
                started=started,
            )
        except stripe.APIConnectionError:
            return failed(
                run_id,
                action_id,
                "Stripe is unavailable",
                ConnectorErrorCode.UNAVAILABLE,
                retryable=True,
                started=started,
            )
        except stripe.APIError:
            return failed(
                run_id,
                action_id,
                "Stripe returned an API error",
                ConnectorErrorCode.UNKNOWN,
                retryable=True,
                started=started,
            )
        except Exception:
            logger.exception("Unexpected Stripe connector failure", extra={"action": action})
            return failed(
                run_id,
                action_id,
                "unexpected Stripe connector failure",
                ConnectorErrorCode.UNKNOWN,
                started=started,
            )
        finally:
            if owned_http_client is not None:
                try:
                    await owned_http_client.close_async()
                except Exception:  # noqa: BLE001 - cleanup must not mask the action result
                    logger.warning("Could not close Stripe HTTP client")

        raise AssertionError(f"unhandled Stripe action: {action}")

    async def _create_checkout_session(
        self,
        client: Any,
        payload: dict[str, Any],
        settings: Settings,
        run_id: str,
        action_id: str,
        started: float,
    ) -> ExecutionResult:
        catalog_key = _text(payload.get("catalog_key"))
        if not catalog_key:
            return failed(run_id, action_id, "catalog_key is required", started=started)
        price_id = settings.STRIPE_PRICE_MAP.get(catalog_key)
        if not price_id or not price_id.startswith("price_"):
            return failed(run_id, action_id, "unknown Stripe catalog key", started=started)

        quantity = payload.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            return failed(run_id, action_id, "quantity must be an integer", started=started)
        if quantity < 1 or quantity > settings.STRIPE_MAX_QUANTITY:
            return failed(
                run_id,
                action_id,
                f"quantity must be between 1 and {settings.STRIPE_MAX_QUANTITY}",
                started=started,
            )

        if not _safe_redirect_url(settings.STRIPE_SUCCESS_URL) or not _safe_redirect_url(
            settings.STRIPE_CANCEL_URL
        ):
            return failed(
                run_id,
                action_id,
                "Stripe redirect URLs must use HTTPS, except for localhost development",
                started=started,
            )

        customer_email = payload.get("customer_email")
        if customer_email is not None and (
            not isinstance(customer_email, str) or not _EMAIL_RE.fullmatch(customer_email.strip())
        ):
            return failed(run_id, action_id, "customer_email is invalid", started=started)

        normalized_customer_email = (
            customer_email.strip() if isinstance(customer_email, str) else None
        )
        checkout_key = checkout_idempotency_key(
            run_id,
            catalog_key=catalog_key,
            price_id=price_id,
            quantity=quantity,
            success_url=settings.STRIPE_SUCCESS_URL,
            cancel_url=settings.STRIPE_CANCEL_URL,
            customer_email=normalized_customer_email,
        )

        params: dict[str, Any] = {
            "mode": "payment",
            "line_items": [{"price": price_id, "quantity": quantity}],
            "success_url": settings.STRIPE_SUCCESS_URL,
            "cancel_url": settings.STRIPE_CANCEL_URL,
            "client_reference_id": run_id,
            # Keep remote parameters stable across an agent replan. The
            # current action ID belongs in local reconciliation metadata; it
            # changes when a replanner emits a replacement step and therefore
            # must not be part of a Stripe-idempotent request.
            "metadata": {
                "run_id": run_id,
                "catalog_key": catalog_key,
                "checkout_idempotency_key": checkout_key,
            },
        }
        if customer_email is not None:
            params["customer_email"] = normalized_customer_email

        session = await client.v1.checkout.sessions.create_async(
            params,
            options={"idempotency_key": checkout_key},
        )
        session_data = _checkout_session_data(session)
        try:
            await self._payments.record_checkout_session(
                {
                    **session_data,
                    "metadata": {"run_id": run_id, "action_id": action_id},
                }
            )
        except Exception as exc:
            logger.exception(
                "Stripe Checkout Session persistence failed",
                extra={
                    "action": "create_checkout_session",
                    "catalog_key": catalog_key,
                    "stripe_session_id": session_data.get("id"),
                    "run_id": run_id or None,
                    "action_id": action_id or None,
                    "idempotency_key": checkout_key,
                },
            )
            return failed(
                run_id,
                action_id,
                "Stripe session was created but local reconciliation failed",
                ConnectorErrorCode.UNAVAILABLE,
                retryable=True,
                details={
                    "stripe_session_id": session_data.get("id"),
                    "stripe_session_url": session_data.get("url"),
                    "idempotency_key": checkout_key,
                    "catalog_key": catalog_key,
                    "reconciliation_error_type": type(exc).__name__,
                },
                started=started,
            )

        return success(
            run_id,
            action_id,
            "Created Stripe Checkout Session",
            session_data,
            started,
        )

    async def _retrieve_checkout_session(
        self,
        client: Any,
        payload: dict[str, Any],
        run_id: str,
        action_id: str,
        started: float,
    ) -> ExecutionResult:
        session_id = _stripe_id(payload.get("session_id"), "cs_")
        if not session_id:
            return failed(run_id, action_id, "valid session_id is required", started=started)
        session = await client.v1.checkout.sessions.retrieve_async(session_id)
        return success(
            run_id,
            action_id,
            "Retrieved Stripe Checkout Session",
            _checkout_session_data(session),
            started,
        )

    async def _expire_checkout_session(
        self,
        client: Any,
        payload: dict[str, Any],
        run_id: str,
        action_id: str,
        started: float,
    ) -> ExecutionResult:
        session_id = _stripe_id(payload.get("session_id"), "cs_")
        if not session_id:
            return failed(run_id, action_id, "valid session_id is required", started=started)
        session = await client.v1.checkout.sessions.expire_async(
            session_id,
            options={"idempotency_key": idempotency_key(run_id, action_id, "expire")},
        )
        return success(
            run_id,
            action_id,
            "Expired Stripe Checkout Session",
            _checkout_session_data(session),
            started,
        )

    async def _create_refund(
        self,
        client: Any,
        payload: dict[str, Any],
        run_id: str,
        action_id: str,
        started: float,
    ) -> ExecutionResult:
        payment_intent_id = _stripe_id(payload.get("payment_intent_id"), "pi_")
        if not payment_intent_id:
            return failed(run_id, action_id, "valid payment_intent_id is required", started=started)

        params: dict[str, Any] = {
            "payment_intent": payment_intent_id,
            "metadata": {"run_id": run_id, "action_id": action_id},
        }
        amount = payload.get("amount")
        if amount is not None:
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
                return failed(
                    run_id,
                    action_id,
                    "refund amount must be a positive integer in the currency's minor unit",
                    started=started,
                )
            params["amount"] = amount
        reason = payload.get("reason")
        if reason is not None:
            if reason not in {"duplicate", "fraudulent", "requested_by_customer"}:
                return failed(run_id, action_id, "invalid refund reason", started=started)
            params["reason"] = reason

        refund = await client.v1.refunds.create_async(
            params,
            options={"idempotency_key": idempotency_key(run_id, action_id, "refund")},
        )
        return success(
            run_id,
            action_id,
            "Created Stripe refund",
            _refund_data(refund),
            started,
        )

    async def _retrieve_refund(
        self,
        client: Any,
        payload: dict[str, Any],
        run_id: str,
        action_id: str,
        started: float,
    ) -> ExecutionResult:
        refund_id = _stripe_id(payload.get("refund_id"), "re_")
        if not refund_id:
            return failed(run_id, action_id, "valid refund_id is required", started=started)
        refund = await client.v1.refunds.retrieve_async(refund_id)
        return success(
            run_id,
            action_id,
            "Retrieved Stripe refund",
            _refund_data(refund),
            started,
        )


def idempotency_key(run_id: str, action_id: str, action: str) -> str:
    return f"agentgate:{run_id}:{action_id}:{action}"[:255]


def checkout_idempotency_key(
    run_id: str,
    *,
    catalog_key: str,
    price_id: str,
    quantity: int,
    success_url: str,
    cancel_url: str,
    customer_email: str | None,
) -> str:
    """Return a stable key for one logical checkout within an agent run.

    Replanned steps receive new action IDs. Using the action ID here would let
    a retry create a second Checkout Session after a local reconciliation
    failure. Hashing the complete remote request inputs keeps the key stable
    for an equivalent retry while avoiding customer PII in the key itself.
    """
    material = json.dumps(
        {
            "run_id": run_id,
            "catalog_key": catalog_key,
            "price_id": price_id,
            "quantity": quantity,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "customer_email": customer_email,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:32]
    return f"agentgate:{run_id}:checkout:{digest}"[:255]


def _checkout_session_data(session: Any) -> dict[str, Any]:
    return {
        "id": _get(session, "id"),
        "url": _get(session, "url"),
        "status": _get(session, "status"),
        "payment_status": _get(session, "payment_status"),
        "payment_intent": _identifier(_get(session, "payment_intent")),
        "amount_total": _get(session, "amount_total"),
        "currency": _get(session, "currency"),
        "expires_at": _get(session, "expires_at"),
    }


def _refund_data(refund: Any) -> dict[str, Any]:
    return {
        "id": _get(refund, "id"),
        "payment_intent": _identifier(_get(refund, "payment_intent")),
        "status": _get(refund, "status"),
        "amount": _get(refund, "amount"),
        "currency": _get(refund, "currency"),
        "reason": _get(refund, "reason"),
    }


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("id")) or None
    return _text(value) or None


def _stripe_id(value: Any, prefix: str) -> str | None:
    identifier = _text(value)
    if not identifier.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_]+", identifier):
        return None
    return identifier


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _safe_redirect_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme == "https" and bool(parsed.netloc):
        return True
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def success(
    run_id: str,
    action_id: str,
    summary: str,
    data: dict[str, Any],
    started: float,
) -> ExecutionResult:
    return ExecutionResult(
        run_id=run_id,
        action_id=action_id,
        executor="stripe",
        status=ExecutionStatus.SUCCESS,
        result_summary=summary,
        data=data,
        latency_ms=int((perf_counter() - started) * 1000),
    )


def failed(
    run_id: str,
    action_id: str,
    message: str,
    code: ConnectorErrorCode = ConnectorErrorCode.VALIDATION,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    started: float,
) -> ExecutionResult:
    error = ConnectorError(
        code=code,
        message=message,
        retryable=retryable,
        details={key: value for key, value in (details or {}).items() if value is not None},
    )
    return ExecutionResult(
        run_id=run_id,
        action_id=action_id,
        executor="stripe",
        status=ExecutionStatus.FAILED,
        result_summary=message,
        error=error.model_dump(mode="json"),
        latency_ms=int((perf_counter() - started) * 1000),
    )
