import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.credentials.local import (
    CredentialError,
    EnvironmentSecrets,
    KeychainSecrets,
    LocalTokenStore,
    SessionSecrets,
)
from app.credentials.oauth import connect_google
from app.runtime.safety import Sanitizer
from app.storage.local.database import LocalDatabase


def test_keychain_rejects_plaintext_backend(tmp_path, monkeypatch):
    import keyring

    backend = type("Plaintext", (), {"__module__": "keyrings.alt.file", "priority": 10})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    with pytest.raises(CredentialError, match="secure OS keychain"):
        KeychainSecrets(tmp_path).get("llm")


def test_environment_store_is_explicit_and_read_only(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "private-key")
    store = EnvironmentSecrets()
    assert store.get("llm") == "private-key"
    assert store.get("oauth.gmail") is None
    with pytest.raises(CredentialError):
        store.set("llm", "replacement")
    with pytest.raises(CredentialError):
        store.delete("llm")


@pytest.mark.asyncio
async def test_oauth_tokens_are_not_stored_in_sqlite(tmp_path):
    secrets, sanitizer = SessionSecrets(), Sanitizer()
    with LocalDatabase(tmp_path / "data" / "state.sqlite3") as database:
        store = LocalTokenStore(database, secrets, sanitizer)
        await store.save("gmail", "private-access", "private-refresh", datetime.now(UTC), "read")
        token = await store.get("gmail")
        assert token.access_token == "private-access"
        metadata = dict(database.db.execute("SELECT * FROM oauth_metadata").fetchone())
        assert "private-access" not in json.dumps(metadata)
        assert "private-refresh" not in json.dumps(metadata)
        assert sanitizer.text("private-access private-refresh") == "[REDACTED] [REDACTED]"


@pytest.mark.asyncio
async def test_desktop_oauth_uses_loopback_state_and_pkce(tmp_path):
    queries, requests, callbacks = [], [], []

    async def browser_callback(url):
        params = parse_qs(urlsplit(url).query)
        queries.append(params)
        redirect = params["redirect_uri"][0]
        assert urlsplit(redirect).hostname == "127.0.0.1"
        async with httpx.AsyncClient(trust_env=False) as client:
            invalid = await client.get(
                redirect, params={"code": "fake-code", "state": "wrong-state"}
            )
            assert invalid.status_code == 400
            response = await client.get(
                redirect, params={"code": "fake-code", "state": params["state"][0]}
            )
            assert response.status_code == 200

    def open_browser(url):
        callbacks.append(asyncio.create_task(browser_callback(url)))
        return True

    def exchange(request):
        payload = parse_qs(request.content.decode())
        requests.append(payload)
        verifier = payload["code_verifier"][0]
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert queries[0]["code_challenge"] == [expected]
        assert payload["redirect_uri"] == queries[0]["redirect_uri"]
        return httpx.Response(
            200,
            json={
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 3600,
            },
        )

    with LocalDatabase(tmp_path / "data" / "state.sqlite3") as database:
        tokens = LocalTokenStore(database, SessionSecrets(), Sanitizer())
        async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as client:
            await connect_google(
                "calendar",
                "desktop-client",
                "user-supplied-desktop-secret",
                tokens,
                client=client,
                open_browser=open_browser,
                report=lambda value: None,
            )
        await asyncio.gather(*callbacks)
        assert (await tokens.get("calendar")).refresh_token == "private-refresh"
        assert queries[0]["scope"] == ["https://www.googleapis.com/auth/calendar.events"]
        assert requests[0]["client_secret"] == ["user-supplied-desktop-secret"]
        assert not requests[0].get("state")
        # Listener is gone after connect, not left as a daemon.
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(queries[0]["redirect_uri"][0])


@pytest.mark.asyncio
async def test_oauth_timeout_closes_callback_listener(tmp_path):
    urls = []
    with LocalDatabase(tmp_path / "data" / "state.sqlite3") as database:
        tokens = LocalTokenStore(database, SessionSecrets(), Sanitizer())
        with pytest.raises(CredentialError, match="timed out"):
            await connect_google(
                "gmail",
                "desktop-client",
                "",
                tokens,
                timeout=0.01,
                open_browser=lambda url: urls.append(url) or True,
                report=lambda value: None,
            )
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(parse_qs(urlsplit(urls[0]).query)["redirect_uri"][0])


@pytest.mark.asyncio
async def test_expired_local_oauth_token_refreshes_in_keychain_only(tmp_path, monkeypatch):
    from app.domains.oauth.service import get_access_token
    from app.runtime.config import LocalConfig, LocalPaths
    from app.runtime.local import LocalRuntime

    secrets = SessionSecrets()
    paths = LocalPaths(tmp_path / "config" / "config.toml", tmp_path / "data")
    config = LocalConfig(
        workspace=str(tmp_path), credential_store="session", google_client_id="desktop-client"
    )
    with LocalRuntime(config, paths, secrets=secrets) as runtime:
        await runtime.tokens.save(
            "gmail",
            "old-access",
            "private-refresh",
            datetime.now(UTC) - timedelta(seconds=1),
            "read",
        )

        def refresh(request):
            assert parse_qs(request.content.decode())["refresh_token"] == ["private-refresh"]
            return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})

        async with httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as client:
            assert await get_access_token("gmail", client=client) == "new-access"
        assert (await runtime.tokens.get("gmail")).refresh_token == "private-refresh"
        assert (
            runtime.sanitizer.text("old-access new-access private-refresh")
            == "[REDACTED] [REDACTED] [REDACTED]"
        )


@pytest.mark.asyncio
async def test_llm_context_masks_known_secrets_without_changing_authentication(
    tmp_path, monkeypatch
):
    from app.llm.services import client
    from app.runtime.config import LocalConfig, LocalPaths
    from app.runtime.local import LocalRuntime

    secrets = SessionSecrets()
    secrets.set("llm", "private-provider-key")
    paths = LocalPaths(tmp_path / "config" / "config.toml", tmp_path / "data")
    config = LocalConfig(workspace=str(tmp_path), credential_store="session")

    async def outbound(payload, settings, fallback_system_prompt):
        assert settings.LLM_API_KEY == "private-provider-key"
        assert payload["messages"][0]["content"] == "[REDACTED] [REDACTED]"
        assert fallback_system_prompt == "[REDACTED]"
        return {"choices": []}, False

    monkeypatch.setattr(client, "_post_openai", outbound)
    with LocalRuntime(config, paths, secrets=secrets) as runtime:
        runtime.sanitizer.remember("private-entered-password")
        await client.post_chat(
            {
                "messages": [
                    {"role": "user", "content": "private-provider-key private-entered-password"}
                ]
            },
            fallback_system_prompt="private-provider-key",
        )
