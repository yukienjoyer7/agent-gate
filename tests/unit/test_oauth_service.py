import asyncio
from datetime import UTC, datetime, timedelta

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


class FakeStateRepo:
    def __init__(self, storage: dict | None = None):
        self.storage = storage if storage is not None else {}

    async def create(self, state_hash, provider, expires_at):
        self.storage[state_hash] = {
            "provider": provider,
            "expires_at": expires_at,
            "used_at": None,
        }

    async def consume(self, state_hash, provider, now):
        row = self.storage.get(state_hash)
        if (
            row is None
            or row["provider"] != provider
            or row["used_at"] is not None
            or row["expires_at"] <= now
        ):
            return False
        row["used_at"] = now
        return True


def test_build_authorize_url_includes_client_id_and_state(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "client123")
    get_settings.cache_clear()

    states = FakeStateRepo()
    url = asyncio.run(service.build_authorize_url("github", state_repo=states))

    assert "client_id=client123" in url
    assert "state=" in url
    state = url.split("state=")[1].split("&")[0]
    assert state not in states.storage
    assert next(iter(states.storage.values()))["provider"] == "github"

    get_settings.cache_clear()


def test_build_authorize_url_for_calendar_uses_calendar_scope_and_redirect(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client")
    get_settings.cache_clear()

    states = FakeStateRepo()
    url = asyncio.run(service.build_authorize_url("calendar", state_repo=states))

    assert "client_id=google-client" in url
    assert "calendar.readonly" not in url
    parsed = httpx.URL(url).params
    assert parsed["scope"] == "https://www.googleapis.com/auth/calendar"
    assert parsed["redirect_uri"] == get_settings().GOOGLE_CALENDAR_OAUTH_REDIRECT_URI
    state = parsed["state"]
    assert state not in states.storage
    assert next(iter(states.storage.values()))["provider"] == "calendar"

    get_settings.cache_clear()


def test_exchange_code_rejects_unknown_state():
    async def run():
        return await service.exchange_code(
            "github",
            "code123",
            "bad-state",
            repo=FakeRepo(),
            state_repo=FakeStateRepo(),
        )

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_exchange_code_stores_token():
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
        state_repo = FakeStateRepo()
        url = await service.build_authorize_url("gmail", state_repo=state_repo)
        state = httpx.URL(url).params["state"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = await service.exchange_code(
                "gmail",
                "authcode",
                state,
                repo=repo,
                client=client,
                state_repo=state_repo,
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
                    datetime.now(UTC) - timedelta(seconds=1),
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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad_verification_code"})

    async def run():
        state_repo = FakeStateRepo()
        url = await service.build_authorize_url("github", state_repo=state_repo)
        state = httpx.URL(url).params["state"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await service.exchange_code(
                "github",
                "stale-code",
                state,
                repo=FakeRepo(),
                client=client,
                state_repo=state_repo,
            )

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_state_survives_service_instance_change_and_is_single_use():
    async def run():
        storage: dict = {}
        authorize_repo = FakeStateRepo(storage)
        callback_repo = FakeStateRepo(storage)
        url = await service.build_authorize_url("github", state_repo=authorize_repo)
        state = httpx.URL(url).params["state"]

        first = await callback_repo.consume(service._hash_state(state), "github", datetime.now(UTC))
        replay = await authorize_repo.consume(
            service._hash_state(state), "github", datetime.now(UTC)
        )
        return first, replay

    assert asyncio.run(run()) == (True, False)


def test_expired_state_is_rejected():
    async def run():
        states = FakeStateRepo()
        state = "expired-state"
        await states.create(
            service._hash_state(state),
            "calendar",
            datetime.now(UTC) - timedelta(seconds=1),
        )
        return await states.consume(service._hash_state(state), "calendar", datetime.now(UTC))

    assert asyncio.run(run()) is False


def test_get_access_token_falls_back_to_static_settings(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "static-pat")
    get_settings.cache_clear()

    async def run():
        return await service.get_access_token("github", repo=FakeRepo())

    assert asyncio.run(run()) == "static-pat"

    get_settings.cache_clear()
