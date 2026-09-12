"""Persistence for Stripe reconciliation state and webhook deduplication."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.stripe_payment import StripePayment, StripeWebhookEvent
from app.database.session import SessionLocal


class StripePaymentStore(Protocol):
    async def record_checkout_session(self, session_data: dict[str, Any]) -> None: ...

    async def process_event(self, event: dict[str, Any]) -> bool: ...


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class StripePaymentRepository:
    """Stores only safe identifiers and aggregate payment state.

    Full Stripe payloads can contain customer information, so they are never
    persisted here. The webhook event table stores only the event ID/type and
    acts as a durable deduplication ledger.
    """

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return _reuse(self._session) if self._session is not None else SessionLocal()

    async def record_checkout_session(self, session_data: dict[str, Any]) -> None:
        session_id = _text(session_data.get("id"))
        if not session_id:
            raise ValueError("Stripe Checkout Session ID is required")

        async with self._scope() as db:
            try:
                row = await _find_payment(db, stripe_session_id=session_id)
                if row is None:
                    row = StripePayment(stripe_session_id=session_id, status="open")
                    db.add(row)
                _apply_checkout_session(row, session_data)
                await db.commit()
            except IntegrityError:
                await db.rollback()
                # Two deliveries/retries may reconcile the same Checkout
                # Session concurrently. The unique Stripe session ID makes
                # the operation safe; merge into the row committed by the
                # winner instead of surfacing a false reconciliation failure.
                row = await _find_payment(db, stripe_session_id=session_id)
                if row is None:
                    raise
                _apply_checkout_session(row, session_data)
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
            except Exception:
                await db.rollback()
                raise

    async def process_event(self, event: dict[str, Any]) -> bool:
        event_id = _text(event.get("id"))
        event_type = _text(event.get("type"))
        if not event_id or not event_type:
            raise ValueError("Stripe webhook event must contain id and type")

        async with self._scope() as db:
            duplicate = await db.scalar(
                select(StripeWebhookEvent.id).where(StripeWebhookEvent.stripe_event_id == event_id)
            )
            if duplicate is not None:
                return False

            try:
                db.add(
                    StripeWebhookEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        livemode=bool(event.get("livemode", False)),
                    )
                )
                # Queries inside event application can autoflush the event
                # ledger, so the race-safe IntegrityError boundary starts
                # before event state is applied rather than only at commit.
                await _apply_event(db, event_type, _event_object(event))
                await db.commit()
            except IntegrityError:
                # Concurrent deliveries of the same Stripe event race on the
                # unique event ID. The winner committed the desired state.
                await db.rollback()
                return False
            except Exception:
                await db.rollback()
                raise
        return True


async def _apply_event(db: AsyncSession, event_type: str, obj: dict[str, Any]) -> None:
    if event_type in {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    }:
        session_id = _text(obj.get("id"))
        if not session_id:
            return
        row = await _find_payment(
            db,
            stripe_session_id=session_id,
            payment_intent_id=_identifier(obj.get("payment_intent")),
        )
        if row is None:
            row = StripePayment(stripe_session_id=session_id, status="unknown")
            db.add(row)
        _apply_checkout_session(row, obj)
        if event_type == "checkout.session.async_payment_succeeded":
            row.status = "paid"
        elif event_type == "checkout.session.async_payment_failed":
            row.status = "failed"
        elif event_type == "checkout.session.expired":
            row.status = "expired"
        return

    if event_type in {"payment_intent.succeeded", "payment_intent.payment_failed"}:
        payment_intent_id = _text(obj.get("id"))
        if not payment_intent_id:
            return
        row = await _find_payment(db, payment_intent_id=payment_intent_id)
        if row is None:
            row = StripePayment(payment_intent_id=payment_intent_id, status="unknown")
            db.add(row)
        row.status = "succeeded" if event_type.endswith("succeeded") else "failed"
        row.amount = _integer(obj.get("amount_received") or obj.get("amount"))
        row.currency = _text(obj.get("currency"))
        _apply_metadata(row, obj.get("metadata"))
        return

    if event_type == "charge.refunded":
        payment_intent_id = _identifier(obj.get("payment_intent"))
        if not payment_intent_id:
            return
        row = await _find_payment(db, payment_intent_id=payment_intent_id)
        if row is None:
            row = StripePayment(payment_intent_id=payment_intent_id, status="unknown")
            db.add(row)
        amount = _integer(obj.get("amount"))
        amount_refunded = _integer(obj.get("amount_refunded"))
        row.status = (
            "refunded"
            if amount is not None and amount_refunded is not None and amount_refunded >= amount
            else "partially_refunded"
        )
        row.amount = amount
        row.currency = _text(obj.get("currency"))
        _apply_metadata(row, obj.get("metadata"))


async def _find_payment(
    db: AsyncSession,
    *,
    stripe_session_id: str | None = None,
    payment_intent_id: str | None = None,
) -> StripePayment | None:
    if stripe_session_id:
        row = await db.scalar(
            select(StripePayment).where(StripePayment.stripe_session_id == stripe_session_id)
        )
        if row is not None:
            return row
    if payment_intent_id:
        return await db.scalar(
            select(StripePayment).where(StripePayment.payment_intent_id == payment_intent_id)
        )
    return None


def _apply_checkout_session(row: StripePayment, obj: dict[str, Any]) -> None:
    row.stripe_session_id = _text(obj.get("id")) or row.stripe_session_id
    row.payment_intent_id = _identifier(obj.get("payment_intent")) or row.payment_intent_id
    row.status = _text(obj.get("payment_status")) or _text(obj.get("status")) or row.status
    amount = _integer(obj.get("amount_total"))
    currency = _text(obj.get("currency"))
    if amount is not None:
        row.amount = amount
    if currency is not None:
        row.currency = currency
    _apply_metadata(row, obj.get("metadata"))


def _apply_metadata(row: StripePayment, metadata: Any) -> None:
    if not isinstance(metadata, dict):
        return
    row.run_id = _text(metadata.get("run_id")) or row.run_id
    row.action_id = _text(metadata.get("action_id")) or row.action_id


def _event_object(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("object"), dict):
        return {}
    return data["object"]


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("id"))
    return _text(value)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
