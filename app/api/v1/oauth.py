from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.domains.oauth.repository import OAuthTokenRepository
from app.domains.oauth.service import build_authorize_url, exchange_code

router = APIRouter(prefix="/oauth", tags=["oauth"])

Provider = Literal["github", "gmail", "calendar"]
PROVIDERS: list[Provider] = ["github", "gmail", "calendar"]


@router.get("/status")
async def status() -> dict:
    """Return connection status for every supported provider."""
    repo = OAuthTokenRepository()
    result: dict[str, dict] = {}
    for p in PROVIDERS:
        token = await repo.get(p)
        result[p] = {
            "connected": token is not None,
            "scope": token.scope if token else None,
        }
    return result


@router.get("/{provider}/authorize")
async def authorize(provider: Provider) -> RedirectResponse:
    """Open this URL in a browser (not Postman) -- it redirects to the
    provider's consent screen, which then redirects back to /callback."""
    return RedirectResponse(build_authorize_url(provider))


@router.get("/{provider}/callback")
async def callback(provider: Provider, code: str, state: str) -> dict:
    try:
        token = await exchange_code(provider, code, state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "provider": provider,
        "connected": True,
        "scope": token.scope,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    }
