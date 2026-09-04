import asyncio

import httpx

from app.domains.connector.gmail.gmail import GmailConnector


def test_gmail_list_messages_uses_messages_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/gmail/v1/users/me/messages"
        assert request.url.params["q"] == "is:unread"
        return httpx.Response(
            200,
            json={
                "messages": [{"id": "1", "threadId": "t1"}, {"id": "2", "threadId": "t2"}],
                "resultSizeEstimate": 2,
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://gmail.googleapis.com/gmail/v1",
        ) as client:
            return await GmailConnector(client).execute(
                "list_messages",
                {"run_id": "run_1", "action_id": "act_1", "query": "is:unread"},
            )

    result = asyncio.run(run())

    assert result.status == "SUCCESS"
    assert len(result.data["messages"]) == 2
    assert result.data["result_size_estimate"] == 2


def test_gmail_list_messages_maps_auth_failure():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(401)),
            base_url="https://gmail.googleapis.com/gmail/v1",
        ) as client:
            return await GmailConnector(client).execute(
                "list_messages",
                {"run_id": "run_1", "action_id": "act_1"},
            )

    result = asyncio.run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "AUTH"
