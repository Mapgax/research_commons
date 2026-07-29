"""get_connection() must be the actual commit/rollback boundary, not a
convention callers have to remember (see CLAUDE.md / bad-day-review 2026-07-29).
"""

from __future__ import annotations

from dataclasses import replace

import research_commons.db_market.connection as market_connection
import research_commons.db_news.connection as news_connection
from research_commons.config import get_settings


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn

    def execute(self, sql, params=None) -> None:
        self._conn.executed_sql.append(str(sql))

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.executed_sql: list[str] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.returned_with_close: bool | None = None

    def getconn(self) -> FakeConn:
        return self._conn

    def putconn(self, conn: FakeConn, close: bool = False) -> None:
        self.returned_with_close = close


def _run_clean_exit(module, monkeypatch) -> tuple[FakeConn, FakePool]:
    conn = FakeConn()
    pool = FakePool(conn)
    monkeypatch.setattr(module, "get_pool", lambda: pool)
    with module.get_connection() as c:
        assert c is conn
    return conn, pool


def _run_exception_exit(module, monkeypatch) -> tuple[FakeConn, FakePool]:
    conn = FakeConn()
    pool = FakePool(conn)
    monkeypatch.setattr(module, "get_pool", lambda: pool)
    try:
        with module.get_connection() as c:
            raise ValueError("simulated failure mid-transaction")
    except ValueError:
        pass
    return conn, pool


def test_market_commits_on_clean_exit(monkeypatch):
    conn, pool = _run_clean_exit(market_connection, monkeypatch)
    assert conn.committed is True
    assert conn.rolled_back is False
    assert pool.returned_with_close is False


def test_market_rolls_back_and_discards_on_exception(monkeypatch):
    conn, pool = _run_exception_exit(market_connection, monkeypatch)
    assert conn.committed is False
    assert conn.rolled_back is True
    assert pool.returned_with_close is True


def test_news_commits_on_clean_exit(monkeypatch):
    conn, pool = _run_clean_exit(news_connection, monkeypatch)
    assert conn.committed is True
    assert conn.rolled_back is False
    assert pool.returned_with_close is False


def test_news_rolls_back_and_discards_on_exception(monkeypatch):
    conn, pool = _run_exception_exit(news_connection, monkeypatch)
    assert conn.committed is False
    assert conn.rolled_back is True
    assert pool.returned_with_close is True


def test_market_role_unset_issues_no_set_role(monkeypatch):
    conn, pool = _run_clean_exit(market_connection, monkeypatch)
    assert not any("SET ROLE" in s for s in conn.executed_sql)


def test_market_role_set_issues_set_role_before_yield(monkeypatch):
    settings = replace(get_settings(), market_role="sector_reader")
    monkeypatch.setattr(market_connection, "get_settings", lambda: settings)
    conn = FakeConn()
    pool = FakePool(conn)
    monkeypatch.setattr(market_connection, "get_pool", lambda: pool)
    with market_connection.get_connection() as c:
        assert c is conn
        # SET ROLE must have already run by the time the caller gets the connection.
        assert any("SET ROLE" in s and "sector_reader" in s for s in conn.executed_sql)
    assert conn.committed is True


def test_news_role_set_issues_set_role_before_yield(monkeypatch):
    settings = replace(get_settings(), news_role="some_role")
    monkeypatch.setattr(news_connection, "get_settings", lambda: settings)
    conn = FakeConn()
    pool = FakePool(conn)
    monkeypatch.setattr(news_connection, "get_pool", lambda: pool)
    with news_connection.get_connection() as c:
        assert c is conn
        assert any("SET ROLE" in s and "some_role" in s for s in conn.executed_sql)
