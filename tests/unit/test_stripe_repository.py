import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models.stripe_payment import StripePayment, StripeWebhookEvent
from app.domains.connector.stripe.repository import StripePaymentRepository


def test_repository_reconciles_checkout_and_deduplicates_webhook_events():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(StripePayment.__table__.create)
            await connection.run_sync(StripeWebhookEvent.__table__.create)

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            repository = StripePaymentRepository(session)
            await repository.record_checkout_session(
                {
                    "id": "cs_test_123",
                    "status": "open",
                    "payment_status": "unpaid",
                    "amount_total": 2500,
                    "currency": "usd",
                    "metadata": {"run_id": "run_1", "action_id": "act_1"},
                }
            )
            event = {
                "id": "evt_123",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_test_123",
                        "payment_intent": "pi_123",
                        "payment_status": "paid",
                        "amount_total": 2500,
                        "currency": "usd",
                        "metadata": {"run_id": "run_1", "action_id": "act_1"},
                    }
                },
            }
            first = await repository.process_event(event)
            second = await repository.process_event(event)

            payment = await session.scalar(
                select(StripePayment).where(StripePayment.stripe_session_id == "cs_test_123")
            )
            event_rows = (await session.scalars(select(StripeWebhookEvent))).all()
            assert payment is not None
            assert payment.payment_intent_id == "pi_123"
            assert payment.status == "paid"
            assert payment.run_id == "run_1"
            assert first is True
            assert second is False
            assert len(event_rows) == 1
        await engine.dispose()

    asyncio.run(run())


def test_repository_merges_out_of_order_payment_intent_and_checkout_events():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(StripePayment.__table__.create)
            await connection.run_sync(StripeWebhookEvent.__table__.create)

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            repository = StripePaymentRepository(session)
            await repository.process_event(
                {
                    "id": "evt_pi_first",
                    "type": "payment_intent.succeeded",
                    "data": {
                        "object": {
                            "id": "pi_out_of_order",
                            "amount_received": 3000,
                            "currency": "usd",
                        }
                    },
                }
            )
            await repository.process_event(
                {
                    "id": "evt_checkout_second",
                    "type": "checkout.session.completed",
                    "data": {
                        "object": {
                            "id": "cs_out_of_order",
                            "payment_intent": "pi_out_of_order",
                            "payment_status": "paid",
                            "amount_total": 3000,
                            "currency": "usd",
                        }
                    },
                }
            )

            rows = (await session.scalars(select(StripePayment))).all()
            assert len(rows) == 1
            assert rows[0].stripe_session_id == "cs_out_of_order"
            assert rows[0].payment_intent_id == "pi_out_of_order"
            assert rows[0].status == "paid"
        await engine.dispose()

    asyncio.run(run())
