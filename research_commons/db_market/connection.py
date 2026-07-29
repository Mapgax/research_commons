"""Connection pool for DB1 (`market`).

We use psycopg2's ``ThreadedConnectionPool`` rather than per-call
``psycopg2.connect`` because:

* The Streamlit dashboard and the Railway worker both fan out across threads.
* Aiven's PgBouncer-fronted endpoint penalises rapid connect/disconnect cycles.
* Re-use of already-TLS-negotiated connections is meaningfully faster.

Always acquire connections via :func:`get_connection` — it's a context manager
that guarantees the connection is returned to the pool (or discarded on error).

Learning note: ``threading.Lock`` here is a plain mutex — it prevents two threads
from simultaneously entering the "create pool" branch. Once ``_pool`` is set,
reads are lock-free. This is the "double-checked locking" pattern, safe in
Python because the GIL makes the assignment atomic.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.pool
import psycopg2.sql

from research_commons.config import get_settings

logger = logging.getLogger(__name__)

_pool_lock = threading.Lock()
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return the singleton connection pool, creating it on first call."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:   # re-check after acquiring lock
            return _pool
        s = get_settings()
        if not s.market_database_url:
            raise RuntimeError(
                "MARKET_DATABASE_URL is not set. "
                "Only pipelines that need market_db should call this."
            )
        logger.info("Creating market_db connection pool (min=%d max=%d)",
                    s.market_pool_min, s.market_pool_max)
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=s.market_pool_min,
            maxconn=s.market_pool_max,
            dsn=s.market_database_url,
        )
    return _pool


@contextmanager
def get_connection() -> Iterator[psycopg2.extensions.connection]:
    """Context-manager: yields a pooled connection, returns it on exit.

    Usage::

        from research_commons.db_market.connection import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    On clean exit the transaction is committed. On exception it's rolled back
    and the connection is returned to the pool with ``close=True`` so a
    poisoned session can't be re-used. Callers do not need to (but may) call
    ``conn.commit()`` themselves — this is the actual commit boundary, not a
    convention callers must remember.

    If ``MARKET_ROLE`` is set, every connection issues ``SET ROLE`` to it
    immediately after connecting (Postgres role-switching, not a new login —
    the connecting user must already be a member of that role). This is
    opt-in and currently a no-op in production: ``MARKET_ROLE`` is unset
    everywhere today, and the one role that exists for this
    (``sector_reader``) has zero GRANTs on any table, so switching to it
    without first granting SELECT on every table a consumer needs would
    break every read. Provisioning that role is a deliberate follow-up, not
    something this connection helper does on its own.
    """
    pool = get_pool()
    conn = pool.getconn()
    ok = False
    try:
        role = get_settings().market_role
        if role:
            with conn.cursor() as cur:
                cur.execute(psycopg2.sql.SQL("SET ROLE {}").format(psycopg2.sql.Identifier(role)))
        yield conn
        conn.commit()
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
    """Close every pooled connection. Call at process shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
            logger.info("market_db connection pool closed.")
