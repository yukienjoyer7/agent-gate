"""Desktop Google authorization using a one-shot loopback callback and PKCE."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import webbrowser
from datetime import UTC, datetime, timedelta
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.credentials.local import CredentialError, LocalTokenStore

SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "calendar": "https://www.googleapis.com/auth/calendar.events",
}


async def connect_google(
    provider: str,
    client_id: str,
    client_secret: str,
    tokens: LocalTokenStore,
    *,
    timeout: float = 180,
    client: httpx.AsyncClient | None = None,
    open_browser: Callable[[str], bool] = webbrowser.open,
    report: Callable[[str], None] = print,
) -> None:
    if not client_id:
        raise CredentialError(
            "Configure google_client_id for a Google Desktop app registration first"
        )
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    handlers: set[asyncio.Task] = set()

    async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            handlers.add(task)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request = line.decode("ascii").strip().split(" ")
            if len(request) != 3 or request[0] != "GET":
                raise ValueError("Invalid callback request")
            url = urlsplit(request[1])
            params = parse_qs(url.query)
            valid = url.path == "/callback" and secrets.compare_digest(
                params.get("state", [""])[0], state
            )
            if not valid:
                status, message = "400 Bad Request", "Invalid authorization callback."
            elif result.done():
                status, message = "409 Conflict", "Authorization callback already received."
            elif params.get("error"):
                result.set_exception(CredentialError("Google authorization was declined or failed"))
                status, message = (
                    "400 Bad Request",
                    "Authorization failed. Return to your terminal.",
                )
            elif params.get("code"):
                result.set_result(params["code"][0])
                status, message = (
                    "200 OK",
                    "Authorization received. Close this tab and return to your terminal.",
                )
            else:
                status, message = "400 Bad Request", "Authorization code missing."
            body = message.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (ValueError, TimeoutError, UnicodeError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            if task is not None:
                handlers.discard(task)

    server = await asyncio.start_server(callback, "127.0.0.1", 0, limit=8192)
    try:
        port = server.sockets[0].getsockname()[1]
        redirect = f"http://127.0.0.1:{port}/callback"
        authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": SCOPES[provider],
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        report("Opening your system browser for Google authorization…")
        if not open_browser(authorization_url):
            report("Open this authorization URL in your system browser:\n" + authorization_url)
        code = await asyncio.wait_for(result, timeout=timeout)
    except TimeoutError as exc:
        raise CredentialError("Google authorization timed out; run connect again") from exc
    finally:
        server.close()
        await server.wait_closed()
        for handler in list(handlers):
            handler.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
    data = {
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if client_secret:
        # Google Desktop credentials can include a secret, but it is not treated
        # as confidential client authentication and is never bundled with AgentGate.
        data["client_secret"] = client_secret

    async def exchange(http: httpx.AsyncClient) -> None:
        response = await http.post("https://oauth2.googleapis.com/token", data=data)
        if not response.is_success:
            raise CredentialError("Google token exchange failed; check Desktop app credentials")
        payload = response.json()
        if not payload.get("access_token"):
            raise CredentialError("Google did not return an access token")
        expires = datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600)))
        await tokens.save(
            provider,
            payload["access_token"],
            payload.get("refresh_token"),
            expires,
            payload.get("scope", SCOPES[provider]),
        )

    if client is not None:
        await exchange(client)
    else:
        async with httpx.AsyncClient(timeout=15) as owned:
            await exchange(owned)


async def disconnect_google(
    provider: str, tokens: LocalTokenStore, client: httpx.AsyncClient | None = None
) -> None:
    token = await tokens.get(provider)
    if token is not None:

        async def revoke(http: httpx.AsyncClient) -> None:
            response = await http.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": token.refresh_token or token.access_token},
            )
            if not response.is_success:
                raise CredentialError(
                    "Google revocation failed; revoke access in your Google account"
                )

        if client is not None:
            await revoke(client)
        else:
            async with httpx.AsyncClient(timeout=15) as owned:
                await revoke(owned)
    tokens.secrets.delete(f"oauth.{provider}")
    with tokens.database.db as db:
        db.execute("DELETE FROM oauth_metadata WHERE provider=?", (provider,))
