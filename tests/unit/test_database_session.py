from __future__ import annotations

import ssl

from app.database import session
from app.database.url import (
    asyncpg_connect_args,
    database_connection_error_summary,
    database_connection_metadata,
    normalize_asyncpg_url,
)


def test_plain_asyncpg_url_preserves_host_and_database() -> None:
    url = normalize_asyncpg_url("postgresql+asyncpg://user:pass@db.example.test/appdb")

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "db.example.test"
    assert url.database == "appdb"
    assert dict(url.query) == {}


def test_neon_asyncpg_url_removes_only_libpq_options() -> None:
    hostname = "ep-example-pooler.us-east-2.aws.neon.tech"
    url = normalize_asyncpg_url(
        "postgresql+asyncpg://user:pass@"
        f"{hostname}/neondb?sslmode=require&channel_binding=require&prepared_statement_cache_size=0"
    )

    assert url.host == hostname
    assert url.database == "neondb"
    assert "sslmode" not in url.query
    assert "channel_binding" not in url.query
    assert url.query["prepared_statement_cache_size"] == "0"


def test_url_normalization_keeps_url_escaped_password_and_neon_host() -> None:
    hostname = "ep-example-pooler.us-east-2.aws.neon.tech"
    url = normalize_asyncpg_url(
        "postgresql+asyncpg://user:pa%2Fss%40word@" f"{hostname}:5432/neondb?sslmode=require"
    )

    assert url.host == hostname
    assert url.port == 5432
    assert url.password == "pa/ss@word"
    assert "sslmode" not in url.query


def test_asyncpg_uses_verified_ssl_context_for_neon() -> None:
    connect_args = asyncpg_connect_args(
        "postgresql+asyncpg://user:pass@ep-example-pooler.us-east-2.aws.neon.tech/neondb"
    )

    ssl_context = connect_args["ssl"]
    assert isinstance(ssl_context, ssl.SSLContext)
    assert ssl_context.check_hostname is True
    assert ssl_context.verify_mode == ssl.CERT_REQUIRED


def test_asyncpg_uses_verified_ssl_context_for_sslmode_require() -> None:
    connect_args = asyncpg_connect_args(
        "postgresql+asyncpg://user:pass@remote.example.test/appdb?sslmode=require"
    )

    assert isinstance(connect_args["ssl"], ssl.SSLContext)


def test_engine_uses_normalized_url_secure_ssl_and_configured_pool(monkeypatch) -> None:
    captured: dict = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(session, "create_async_engine", fake_create_async_engine)
    session.create_database_engine(
        "postgresql+asyncpg://user:pass@ep-example-pooler.us-east-2.aws.neon.tech/"
        "neondb?sslmode=require&channel_binding=require",
        pool_size=7,
        max_overflow=11,
    )

    assert captured["url"].host == "ep-example-pooler.us-east-2.aws.neon.tech"
    assert "sslmode" not in captured["url"].query
    assert "channel_binding" not in captured["url"].query
    assert isinstance(captured["kwargs"]["connect_args"]["ssl"], ssl.SSLContext)
    assert captured["kwargs"]["pool_pre_ping"] is True
    assert captured["kwargs"]["pool_recycle"] == session.POOL_RECYCLE_SECONDS
    assert captured["kwargs"]["pool_size"] == 7
    assert captured["kwargs"]["max_overflow"] == 11


def test_connection_diagnostics_never_render_credentials_or_full_url() -> None:
    database_url = "postgresql+asyncpg://user:super-secret@db.example.test/appdb"
    metadata = database_connection_metadata(database_url)
    summary = database_connection_error_summary(
        database_url, RuntimeError("postgresql+asyncpg://user:super-secret@db.example.test/appdb")
    )

    assert metadata.host == "db.example.test"
    assert metadata.database == "appdb"
    assert "super-secret" not in summary
    assert database_url not in summary
    assert "exception=RuntimeError" in summary
