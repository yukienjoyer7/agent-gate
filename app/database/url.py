"""Safe database URL handling shared by runtime and migration code."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.engine import URL, make_url

DatabaseSSLMode = Literal["auto", "require", "disable"]

# These are libpq/psycopg options. SQLAlchemy's asyncpg dialect forwards URL
# query arguments to asyncpg.connect(), which does not accept either option.
_ASYNCPG_UNSUPPORTED_QUERY_KEYS = frozenset({"sslmode", "channel_binding"})
_TLS_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})


@dataclass(frozen=True)
class DatabaseConnectionMetadata:
    """Non-sensitive database connection details suitable for diagnostics."""

    drivername: str
    host: str | None
    port: int | None
    database: str | None
    query_keys: tuple[str, ...]


def normalize_asyncpg_url(database_url: str | URL) -> URL:
    """Remove libpq-only query options from an asyncpg SQLAlchemy URL.

    ``URL.set`` keeps every URL component (including URL-escaped credentials)
    intact. It avoids the hostname corruption that can result from rebuilding
    connection strings by hand.
    """

    url = make_url(database_url)
    if url.drivername != "postgresql+asyncpg":
        return url

    query = dict(url.query)
    for key in _ASYNCPG_UNSUPPORTED_QUERY_KEYS:
        query.pop(key, None)
    return url.set(query=query)


def should_use_asyncpg_ssl(database_url: str | URL, ssl_mode: DatabaseSSLMode = "auto") -> bool:
    """Determine whether the asyncpg connection needs a verified TLS context."""

    url = make_url(database_url)
    if url.drivername != "postgresql+asyncpg":
        return False
    if ssl_mode == "require":
        return True
    if ssl_mode == "disable":
        return False

    query = url.query
    url_sslmode = str(query.get("sslmode", "")).lower()
    url_ssl = str(query.get("ssl", "")).lower()
    hostname = (url.host or "").rstrip(".").lower()

    # Explicit URL options take priority. The Neon suffix is an automatic
    # fallback for a provider URL without its libpq query string. Other remote
    # providers can set DATABASE_SSL_MODE=require explicitly.
    return (
        url_sslmode in _TLS_SSLMODES
        or url_ssl in _TLS_SSLMODES | {"true", "1"}
        or hostname.endswith(".neon.tech")
    )


def asyncpg_connect_args(
    database_url: str | URL, ssl_mode: DatabaseSSLMode = "auto"
) -> dict[str, ssl.SSLContext]:
    """Return asyncpg-specific arguments without weakening certificate checks."""

    if not should_use_asyncpg_ssl(database_url, ssl_mode):
        return {}
    return {"ssl": ssl.create_default_context()}


def database_connection_metadata(database_url: str | URL) -> DatabaseConnectionMetadata:
    """Return safe URL metadata without rendering credentials or the full URL."""

    url = make_url(database_url)
    return DatabaseConnectionMetadata(
        drivername=url.drivername,
        host=url.host,
        port=url.port,
        database=url.database,
        query_keys=tuple(sorted(url.query.keys())),
    )


def database_connection_error_summary(database_url: str | URL, error: BaseException) -> str:
    """Format a safe connectivity error without including exception text.

    Driver exception text can contain arbitrary connection details, so the
    report intentionally contains only parsed non-secret metadata and the
    exception class.
    """

    metadata = database_connection_metadata(database_url)
    return (
        "Database connection failed: "
        f"driver={metadata.drivername} host={metadata.host!r} "
        f"port={metadata.port or 5432} database={metadata.database!r} "
        f"exception={type(error).__name__}"
    )
