"""DB2 — news & text intelligence (Aiven Postgres)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extensions

from research_commons.db_news.connection import (
    close_pool,
    get_connection,
    get_pool,
)
from research_commons.db_news.ddl import create_tables


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


def init_news_db(database_url: str | None = None) -> None:
    """Create or patch the news DB schema using the bundled SQL migrations.

    What you can learn from this API:
    - A public initialization entrypoint removes guesswork for operational code.
    - Accepting an optional explicit URL makes scripts easy to test without
      mutating process-wide environment settings.
    - The actual DDL still lives in SQL migrations, so this function stays a
      thin orchestration layer rather than becoming a second schema source.

    Security note:
    - This function connects to PostgreSQL and therefore sends data over the
      network to the configured database host.
    - It does not contact third-party APIs.
    """
    with _init_connection(database_url) as conn:
        create_tables(conn)


__all__ = ["get_pool", "get_connection", "close_pool", "init_news_db"]
