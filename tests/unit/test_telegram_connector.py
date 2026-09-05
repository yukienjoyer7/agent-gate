import asyncio
import json
import logging

import httpx

from app.domains.connector.telegram.telegram import TelegramConnector, split_telegram_text


def _run(coro):
    return asyncio.run(coro)


def _payload(**overrides):
    payload = {
        "run_id": "run_1",
        "action_id": "act_1",
        "chat_id": "123",
        "text": "hello",
    }
    payload.update(overrides)
    return payload


def test_send_message_success_uses_telegram_endpoint_and_payload():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/bottest-token/sendMessage"
        body = json.loads(request.content.decode())
        assert body["chat_id"] == 123
        assert body["text"] == "hello"
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": 123}}},
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message",
                _payload(),
            )

    result = _run(run())

    assert len(requests) == 1
    assert result.status == "SUCCESS"
    assert result.data["chat_id"] == 123
    assert result.data["message_id"] == 42
    assert result.data["message_ids"] == [42]


def test_missing_token_fails_safely():
    result = _run(TelegramConnector(token="").execute("send_message", _payload()))

    assert result.status == "FAILED"
    assert result.error["code"] == "AUTH"
    assert "token" in result.error["message"].lower()


def test_missing_chat_id_fails_validation():
    result = _run(
        TelegramConnector(token="test-token").execute("send_message", _payload(chat_id=""))
    )

    assert result.status == "FAILED"
    assert result.error["code"] == "VALIDATION"
    assert "chat_id" in result.error["message"]


def test_unresolved_human_name_chat_id_is_rejected_without_an_http_request():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.telegram.test"
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message", _payload(chat_id="Rafi Ahmad")
            )

    result = _run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "VALIDATION"
    assert requests == []


def test_missing_text_fails_validation():
    result = _run(TelegramConnector(token="test-token").execute("send_message", _payload(text="")))

    assert result.status == "FAILED"
    assert result.error["code"] == "VALIDATION"
    assert "text" in result.error["message"]


def test_unsupported_action_fails_validation():
    result = _run(TelegramConnector(token="test-token").execute("unknown", _payload()))

    assert result.status == "FAILED"
    assert result.error["code"] == "VALIDATION"
    assert "unsupported" in result.result_summary


def test_timeout_maps_to_retryable_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message",
                _payload(),
            )

    result = _run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "TIMEOUT"
    assert result.error["retryable"] is True


def test_network_error_maps_to_retryable_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed", request=request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message",
                _payload(),
            )

    result = _run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "UNAVAILABLE"
    assert result.error["retryable"] is True


def test_non_2xx_response_maps_to_connector_error():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message",
                _payload(),
            )

    result = _run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "UNAVAILABLE"
    assert result.error["retryable"] is True
    assert result.error["details"]["status_code"] == 500


def test_ok_false_response_maps_to_connector_error():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"ok": False, "error_code": 400, "description": "Bad Request"},
                )
            ),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message",
                _payload(),
            )

    result = _run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "UNKNOWN"
    assert "Bad Request" in result.error["message"]


def test_malformed_response_maps_to_safe_error():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json")),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message",
                _payload(),
            )

    result = _run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "UNKNOWN"
    assert "malformed" in result.error["message"]


def test_token_never_appears_in_returned_errors():
    token = "123456:SECRET-TOKEN"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "ok": False,
                        "description": f"bad token {token}",
                    },
                )
            ),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token=token).execute("send_message", _payload())

    result = _run(run())

    dumped = json.dumps(result.model_dump(mode="json"))
    assert token not in dumped
    assert "[REDACTED]" in dumped


def test_token_never_appears_in_httpx_logs(caplog):
    token = "123456:LOG-SECRET"
    caplog.set_level(logging.INFO, logger="httpx")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    500,
                    request=request,
                    text="unavailable",
                )
            ),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token=token).execute("send_message", _payload())

    result = _run(run())

    assert result.status == "FAILED"
    assert token not in caplog.text
    assert "/bot[REDACTED]/sendMessage" in caplog.text


def test_send_message_chunks_long_text():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": len(sent), "chat": {"id": "123"}}},
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.telegram.test",
        ) as client:
            return await TelegramConnector(client, token="test-token").execute(
                "send_message",
                _payload(text=("hello\n" * 900)),
            )

    result = _run(run())

    assert result.status == "SUCCESS"
    assert result.data["chunks"] > 1
    assert all(len(item["text"]) <= 4000 for item in sent)


def test_split_telegram_text_prefers_newline_boundaries():
    text = ("alpha\n" * 5) + ("beta " * 20)
    chunks = split_telegram_text(text, limit=30)

    assert len(chunks) > 1
    assert all(len(chunk) <= 30 for chunk in chunks)
