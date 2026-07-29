"""DB1 — market & fundamentals (Aiven Postgres)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extensions

from research_commons.db_market.connection import (
    get_pool,
    get_connection,
    close_pool,
)
from research_commons.db_market.ddl import create_tables


@contextmanager
def _init_connection(
    database_url: str | None = None,
) -> Iterator[psycopg2.extensions.connection]:
    if database_url:
        conn = psycopg2.connect(database_url, connect_timeout=15)
        try:
            yield conn
        finally:
            conn.close()
        return

    with get_connection() as conn:
        yield conn


def init_market_db(database_url: str | None = None) -> None:
    """Create or patch the market DB schema using the bundled SQL migrations.

    Mirrors ``init_news_db``: a public initialization entrypoint, an optional
    explicit URL for scripts/tests, and all DDL kept in the SQL migrations.

    Security note:
    - This function connects to PostgreSQL and therefore sends data over the
      network to the configured database host.
    - It does not contact third-party APIs.
    """
    with _init_connection(database_url) as conn:
        create_tables(conn)


__all__ = ["get_pool", "get_connection", "close_pool", "init_market_db"]
