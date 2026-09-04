from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.config.settings import get_settings
from app.domains.oauth.repository import OAuthTokenRepository, StoredToken


@dataclass(frozen=True)
class ProviderConfig:
    authorize_url: str
    token_url: str
    scope: str
    client_id: str
    client_secret: str
    redirect_uri: str
    extra_authorize_params: dict[str, str]


def _config(provider: str) -> ProviderConfig:
    settings = get_settings()
    if provider == "github":
        return ProviderConfig(
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            scope="repo",
            client_id=settings.GITHUB_OAUTH_CLIENT_ID,
            client_secret=settings.GITHUB_OAUTH_CLIENT_SECRET,
            redirect_uri=settings.GITHUB_OAUTH_REDIRECT_URI,
            extra_authorize_params={},
        )
    if provider == "gmail":
        return ProviderConfig(
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
            # offline+consent so Google actually returns a refresh_token.
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )
    raise ValueError(f"unknown OAuth provider: {provider}")


# ponytail: in-memory, single-process state store -- fine for a local dev
# prototype where authorize -> callback happens within seconds; move to
# Redis/DB if this runs behind multiple workers.
_pending_states: dict[str, str] = {}


def build_authorize_url(provider: str) -> str:
    config = _config(provider)
    state = secrets.token_urlsafe(16)
    _pending_states[state] = provider
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": config.scope,
        "state": state,
        "response_type": "code",
        **config.extra_authorize_params,
    }
    return str(httpx.URL(config.authorize_url, params=params))


async def exchange_code(
    provider: str,
    code: str,
    state: str,
    repo: OAuthTokenRepository | None = None,
    client: httpx.AsyncClient | None = None,
) -> StoredToken:
    if _pending_states.pop(state, None) != provider:
        raise ValueError("invalid or expired OAuth state")

    config = _config(provider)
    return await _request_token(
        provider,
        {"code": code, "grant_type": "authorization_code", "redirect_uri": config.redirect_uri},
        repo,
        client,
    )


async def get_access_token(
    provider: str,
    repo: OAuthTokenRepository | None = None,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    repo = repo or OAuthTokenRepository()
    token = await repo.get(provider)
    if token is None:
        return _fallback_token(provider)
    if token.expires_at and token.expires_at <= datetime.now(timezone.utc):
        token = await _refresh(provider, token, repo, client)
    return token.access_token


async def _refresh(
    provider: str,
    token: StoredToken,
    repo: OAuthTokenRepository | None,
    client: httpx.AsyncClient | None,
) -> StoredToken:
    if not token.refresh_token:
        return token  # nothing to refresh with (e.g. GitHub classic OAuth) -- keep using it
    return await _request_token(
        provider,
        {"refresh_token": token.refresh_token, "grant_type": "refresh_token"},
        repo,
        client,
        fallback_refresh_token=token.refresh_token,
        fallback_scope=token.scope,
    )


async def _request_token(
    provider: str,
    grant_params: dict[str, str],
    repo: OAuthTokenRepository | None,
    client: httpx.AsyncClient | None,
    fallback_refresh_token: str | None = None,
    fallback_scope: str | None = None,
) -> StoredToken:
    config = _config(provider)
    data = {"client_id": config.client_id, "client_secret": config.client_secret, **grant_params}
    headers = {"Accept": "application/json"}

    if client is not None:
        response = await client.post(config.token_url, data=data, headers=headers)
    else:
        async with httpx.AsyncClient(timeout=10) as owned_client:
            response = await owned_client.post(config.token_url, data=data, headers=headers)

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Surface as a 400 with the provider's own message instead of a bare
        # 500 -- expired/reused code, bad client secret, redirect_uri
        # mismatch all land here.
        raise ValueError(f"{provider} token exchange failed: {response.text}") from exc

    payload = response.json()

    if "error" in payload:
        raise ValueError(payload.get("error_description", payload["error"]))

    expires_at = None
    if payload.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"]))

    return await (repo or OAuthTokenRepository()).save(
        provider=provider,
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", fallback_refresh_token),
        expires_at=expires_at,
        scope=payload.get("scope", fallback_scope or config.scope),
    )


def _fallback_token(provider: str) -> str | None:
    settings = get_settings()
    if provider == "github":
        return settings.GITHUB_TOKEN or None
    if provider == "gmail":
        return settings.GMAIL_ACCESS_TOKEN or None
    return None
