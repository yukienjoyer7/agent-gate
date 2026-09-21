from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app.domains.oauth.repository import OAuthTokenRepository
from app.domains.oauth.service import build_authorize_url, exchange_code

router = APIRouter(prefix="/oauth", tags=["oauth"])

Provider = Literal["github", "gmail", "calendar"]
PROVIDERS: list[Provider] = ["github", "gmail", "calendar"]


class OAuthProviderStatus(BaseModel):
    connected: bool = Field(
        ...,
        description="Whether a valid OAuth token is stored for this provider",
        examples=[True],
    )
    scope: str | None = Field(
        default=None,
        description="OAuth scopes granted for this provider",
        examples=["repo,user"],
    )


class OAuthCallbackResponse(BaseModel):
    provider: Provider = Field(..., description="OAuth provider name", examples=["github"])
    connected: bool = Field(default=True, description="Connection success status", examples=[True])
    scope: str | None = Field(
        default=None, description="Granted OAuth scope", examples=["repo,user"]
    )
    expires_at: str | None = Field(
        default=None,
        description="ISO 8601 format token expiration timestamp",
    )


@router.get(
    "/status",
    response_model=dict[str, OAuthProviderStatus],
    summary="Get OAuth Connections Status",
    description="Return connection status and granted scopes for every supported OAuth provider (github, gmail, calendar).",
)
async def status() -> dict[str, OAuthProviderStatus]:
    """Return connection status for every supported provider."""
    repo = OAuthTokenRepository()
    result: dict[str, OAuthProviderStatus] = {}
    for p in PROVIDERS:
        token = await repo.get(p)
        result[p] = OAuthProviderStatus(
            connected=token is not None,
            scope=token.scope if token else None,
        )
    return result


@router.get(
    "/{provider}/authorize",
    summary="Start OAuth Authorization Flow",
    description="Redirect the user's browser to the specified OAuth provider's consent screen.",
    responses={
        307: {"description": "Redirect to provider OAuth consent page"},
    },
)
async def authorize(
    provider: Provider = Path(
        ..., description="OAuth provider name ('github', 'gmail', 'calendar')", examples=["github"]
    ),
) -> RedirectResponse:
    """Open this URL in a browser (not Postman) -- it redirects to the
    provider's consent screen, which then redirects back to /callback."""
    try:
        authorize_url = await build_authorize_url(provider)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="OAuth state storage is unavailable") from exc
    return RedirectResponse(authorize_url)


@router.get(
    "/{provider}/callback",
    response_model=OAuthCallbackResponse,
    summary="OAuth Provider Callback",
    description="Handle the OAuth redirect callback with authorization code and state token, exchange for access token, and store credentials.",
    responses={
        400: {"description": "OAuth token exchange error or invalid state"},
    },
)
async def callback(
    provider: Provider = Path(..., description="OAuth provider name", examples=["github"]),
    code: str = Query(..., description="Authorization code returned by the OAuth provider"),
    state: str = Query(..., description="CSRF state token returned by the OAuth provider"),
) -> OAuthCallbackResponse:
    try:
        token = await exchange_code(provider, code, state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="OAuth state storage is unavailable") from exc

    return OAuthCallbackResponse(
        provider=provider,
        connected=True,
        scope=token.scope,
        expires_at=token.expires_at.isoformat() if token.expires_at else None,
    )
