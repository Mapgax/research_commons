"""Connection pool for DB2 (`news`).

Mirrors the structure of ``db_market.connection`` exactly. The pool is
*separate* — the two databases live in different Aiven databases and must NOT
share a pool because cross-DB transactions don't work and would silently break
atomicity guarantees.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.pool

from research_commons.config import get_settings

logger = logging.getLogger(__name__)

_pool_lock = threading.Lock()
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        s = get_settings()
        logger.info("Creating news_db connection pool (min=%d max=%d)",
                    s.news_pool_min, s.news_pool_max)
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=s.news_pool_min,
            maxconn=s.news_pool_max,
            dsn=s.news_database_url,
        )
    return _pool


@contextmanager
def get_connection() -> Iterator[psycopg2.extensions.connection]:
    """Context-manager: yields a pooled connection, returns it on exit."""
    pool = get_pool()
    conn = pool.getconn()
    ok = False
    try:
        yield conn
        ok = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        pool.putconn(conn, close=(not ok))


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
            logger.info("news_db connection pool closed.")
