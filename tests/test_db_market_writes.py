"""Unit tests for upsert_ticker_metadata (no real DB — house FakeConnection)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest

import research_commons.db_market.writes as writes


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        self.rowcount = 1

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCursor, FakeConnection]:
    cursor = FakeCursor()
    conn = FakeConnection(cursor)

    @contextmanager
    def fake_get_connection() -> Iterator[FakeConnection]:
        yield conn

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    def fake_execute_values(
        cur: FakeCursor, sql: str, values: list[tuple[Any, ...]], page_size: int = 100
    ) -> None:
        cur.execute(sql, values)
        cur.rowcount = len(values)

    monkeypatch.setattr(writes, "execute_values", fake_execute_values)
    return cursor, conn


def test_upsert_ticker_metadata_empty_is_noop(fake_db: tuple[FakeCursor, FakeConnection]) -> None:
    cursor, conn = fake_db
    assert writes.upsert_ticker_metadata([]) == 0
    assert not cursor.executed
    assert not conn.committed


def test_upsert_ticker_metadata_sql_and_values(
    fake_db: tuple[FakeCursor, FakeConnection],
) -> None:
    cursor, conn = fake_db
    count = writes.upsert_ticker_metadata(
        [
            {
                "ticker": "ROG",
                "company_name": "Roche Holding AG",
                "isin": "CH0012032048",
                "bloomberg_ticker": "ROG SW Equity",
                "yahoo_symbol": "ROG.SW",
                "sector_name": "Health Care",
                "currency": "CHF",
                "scope_status": "watchlist",
            },
            {
                "ticker": "TSLA",
                "company_name": "TSLA",
                "isin": "",
                "yahoo_symbol": "TSLA",
                "currency": "USD",
            },
        ]
    )
    assert count == 2
    assert conn.committed
    sql, values = cursor.executed[0]
    assert "ON CONFLICT (ticker) DO UPDATE" in sql
    assert "updated_at       = now()" in sql
    assert "sector_name" in sql and "sector," not in sql
    # Retired audience flags must not reappear in the write path.
    assert "scope_pm_ch" not in sql and "scope_advisory" not in sql
    roche = values[0]
    assert roche == (
        "ROG", "Roche Holding AG", "CH0012032048", "ROG SW Equity", "ROG.SW",
        "Health Care", "CHF", "watchlist",
    )
    tsla = values[1]
    # Empty strings become NULLs; the status default is fail-safe.
    assert tsla[2] is None and tsla[3] is None
    assert tsla[7] == "watchlist"
