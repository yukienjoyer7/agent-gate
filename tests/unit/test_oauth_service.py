import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config.settings import get_settings
from app.domains.oauth import service
from app.domains.oauth.repository import StoredToken


class FakeRepo:
    def __init__(self, initial: dict[str, StoredToken] | None = None):
        self.store = dict(initial or {})

    async def get(self, provider):
        return self.store.get(provider)

    async def save(self, provider, access_token, refresh_token, expires_at, scope):
        token = StoredToken(access_token, refresh_token, expires_at, scope)
        self.store[provider] = token
        return token


@pytest.fixture(autouse=True)
def _clear_pending_states():
    service._pending_states.clear()
    yield
    service._pending_states.clear()


def test_build_authorize_url_includes_client_id_and_state(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "client123")
    get_settings.cache_clear()

    url = service.build_authorize_url("github")

    assert "client_id=client123" in url
    assert "state=" in url
    state = url.split("state=")[1].split("&")[0]
    assert service._pending_states[state] == "github"

    get_settings.cache_clear()


def test_exchange_code_rejects_unknown_state():
    async def run():
        return await service.exchange_code("github", "code123", "bad-state", repo=FakeRepo())

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_exchange_code_stores_token():
    service._pending_states["state-1"] = "gmail"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "at1",
                "refresh_token": "rt1",
                "expires_in": 3600,
                "scope": "gmail.readonly",
            },
        )

    async def run():
        repo = FakeRepo()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = await service.exchange_code(
                "gmail", "authcode", "state-1", repo=repo, client=client
            )
        return token, repo

    token, repo = asyncio.run(run())

    assert token.access_token == "at1"
    assert token.refresh_token == "rt1"
    assert asyncio.run(repo.get("gmail")).access_token == "at1"


def test_get_access_token_refreshes_expired_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/token")
        return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})

    async def run():
        repo = FakeRepo(
            {
                "gmail": StoredToken(
                    "stale",
                    "rt1",
                    datetime.now(timezone.utc) - timedelta(seconds=1),
                    "scope",
                )
            }
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await service.get_access_token("gmail", repo=repo, client=client)

    assert asyncio.run(run()) == "at2"


def test_exchange_code_raises_value_error_on_provider_error_response():
    """A non-2xx from the provider's token endpoint (expired/reused code,
    bad secret, ...) must surface as a ValueError -> 400, not an uncaught
    httpx.HTTPStatusError -> 500."""
    service._pending_states["state-2"] = "github"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad_verification_code"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await service.exchange_code(
                "github", "stale-code", "state-2", repo=FakeRepo(), client=client
            )

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_get_access_token_falls_back_to_static_settings(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "static-pat")
    get_settings.cache_clear()

    async def run():
        return await service.get_access_token("github", repo=FakeRepo())

    assert asyncio.run(run()) == "static-pat"

    get_settings.cache_clear()
