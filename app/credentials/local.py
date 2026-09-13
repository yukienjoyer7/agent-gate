"""Secure OS keychain, explicit environment, and process-only credentials."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.domains.oauth.repository import StoredToken
from app.runtime.safety import Sanitizer
from app.storage.local.database import LocalDatabase

SECRET_ENV = {
    "llm": "LLM_API_KEY",
    "github": "GITHUB_TOKEN",
    "gmail": "GMAIL_ACCESS_TOKEN",
    "calendar": "GOOGLE_CALENDAR_ACCESS_TOKEN",
    "stripe": "STRIPE_SECRET_KEY",
    "google_client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
}


class CredentialError(ValueError):
    pass


class SecretStore(Protocol):
    writable: bool

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class EnvironmentSecrets:
    writable = False

    def get(self, name: str) -> str | None:
        variable = SECRET_ENV.get(name)
        return os.environ.get(variable) or None if variable else None

    def set(self, name: str, value: str) -> None:
        raise CredentialError(
            "Environment credentials are read-only; set the documented variable yourself"
        )

    def delete(self, name: str) -> None:
        raise CredentialError("Unset the credential environment variable yourself")


class SessionSecrets:
    writable = True

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class KeychainSecrets:
    writable = True

    def __init__(self, profile: Path) -> None:
        self.service = "agentgate:" + hashlib.sha256(str(profile).encode()).hexdigest()[:16]
        self._backend = None

    def _secure_backend(self):
        if self._backend is None:
            import keyring

            backend = keyring.get_keyring()
            candidates = getattr(backend, "backends", [backend])
            for candidate in candidates:
                module = type(candidate).__module__
                if candidate.priority > 0 and module.startswith(
                    (
                        "keyring.backends.SecretService",
                        "keyring.backends.macOS",
                        "keyring.backends.Windows",
                        "keyring.backends.kwallet",
                    )
                ):
                    self._backend = candidate
                    break
            if self._backend is None:
                raise CredentialError(
                    "No supported secure OS keychain is available. "
                    "Use '--credential-store env' or '--credential-store session' explicitly"
                )
        return self._backend

    def get(self, name: str) -> str | None:
        try:
            return self._secure_backend().get_password(self.service, name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                "OS keychain could not be read; unlock it or choose an explicit alternative"
            ) from exc

    def set(self, name: str, value: str) -> None:
        try:
            self._secure_backend().set_password(self.service, name, value)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("OS keychain could not save the credential") from exc

    def delete(self, name: str) -> None:
        try:
            backend = self._secure_backend()
            if backend.get_password(self.service, name) is not None:
                backend.delete_password(self.service, name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("OS keychain could not remove the credential") from exc


def secret_store(mode: str, profile: Path) -> SecretStore:
    if mode == "env":
        return EnvironmentSecrets()
    if mode == "session":
        return SessionSecrets()
    if mode == "keyring":
        return KeychainSecrets(profile)
    raise CredentialError("Unknown credential provider")


class LocalTokenStore:
    def __init__(self, database: LocalDatabase, secrets: SecretStore, sanitizer: Sanitizer) -> None:
        self.database, self.secrets, self.sanitizer = database, secrets, sanitizer

    async def get(self, provider: str) -> StoredToken | None:
        encoded = self.secrets.get(f"oauth.{provider}")
        if encoded is None:
            return None
        payload = json.loads(encoded)
        self.sanitizer.remember(payload["access_token"])
        self.sanitizer.remember(payload.get("refresh_token"))
        return StoredToken(
            payload["access_token"],
            payload.get("refresh_token"),
            datetime.fromisoformat(payload["expires_at"]) if payload.get("expires_at") else None,
            payload.get("scope"),
        )

    async def save(
        self,
        provider: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime | None,
        scope: str | None,
    ) -> StoredToken:
        old = await self.get(provider)
        refresh_token = refresh_token or (old.refresh_token if old else None)
        payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "scope": scope,
        }
        self.secrets.set(f"oauth.{provider}", json.dumps(payload))
        self.sanitizer.remember(access_token)
        self.sanitizer.remember(refresh_token)
        with self.database.db as db:
            db.execute(
                "INSERT INTO oauth_metadata VALUES(?,?,?) ON CONFLICT(provider) "
                "DO UPDATE SET expires_at=excluded.expires_at,scope=excluded.scope",
                (provider, payload["expires_at"], scope),
            )
        return StoredToken(access_token, refresh_token, expires_at, scope)
