from __future__ import annotations

from contextlib import contextmanager

from research_commons.db_news import writes


class FakeCursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql_text: str, params: object = None) -> None:
        self.executed.append((sql_text, params))


class FakeConnection:
    def __init__(self, rowcount: int) -> None:
        self.cursor_obj = FakeCursor(rowcount)
        self.committed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True


def test_refresh_sentiment_daily_populates_source_provenance(monkeypatch) -> None:
    connection = FakeConnection(rowcount=3)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    count = writes.refresh_sentiment_daily()

    assert count == 3
    assert connection.committed is True

    sql_text, params = connection.cursor_obj.executed[0]
    assert "source_count" in sql_text
    assert "source_tiers_present" in sql_text
    assert "JOIN sources s ON s.name = a.source" in sql_text
    assert "count(DISTINCT a.source)" in sql_text
    assert "array_agg(DISTINCT s.reliability_tier)" in sql_text
    assert params == {}


def test_refresh_sentiment_daily_since_filter_passes_through(monkeypatch) -> None:
    from datetime import date

    connection = FakeConnection(rowcount=1)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    cutoff = date(2026, 1, 1)
    count = writes.refresh_sentiment_daily(since=cutoff)

    assert count == 1
    sql_text, params = connection.cursor_obj.executed[0]
    assert "a.published_at >= %(since)s" in sql_text
    assert params == {"since": cutoff}


def test_upsert_ticker_alias_normalizes_and_passes_through(monkeypatch) -> None:
    connection = FakeConnection(rowcount=0)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    writes.upsert_ticker_alias("Apple Inc.", "AAPL", source="llm_extraction")

    assert connection.committed is True
    sql_text, params = connection.cursor_obj.executed[0]
    assert "ticker_aliases" in sql_text
    assert "ON CONFLICT (alias) DO UPDATE" in sql_text
    assert params == {"alias": "apple inc.", "ticker": "AAPL", "source": "llm_extraction"}
