"""Safely verify the configured database with a non-mutating SELECT 1 query."""

from __future__ import annotations

import asyncio

from app.config.settings import get_settings
from app.database.session import check_database_connection, engine
from app.database.url import database_connection_error_summary


async def main() -> int:
    try:
        metadata = await check_database_connection()
    except Exception as error:  # noqa: BLE001 - report every driver failure without exposing its message
        print(database_connection_error_summary(get_settings().DATABASE_URL, error))
        return 1
    finally:
        await engine.dispose()

    print(
        "Database connection OK: "
        f"driver={metadata.drivername} host={metadata.host!r} "
        f"port={metadata.port or 5432} database={metadata.database!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
