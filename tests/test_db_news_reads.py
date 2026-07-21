from __future__ import annotations

from contextlib import contextmanager

from research_commons.db_news import reads


class FakeCursor:
    def __init__(self, row) -> None:
        self.row = row
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql_text: str, params: object = None) -> None:
        self.executed.append((sql_text, params))

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row) -> None:
        self.cursor_obj = FakeCursor(row)

    def cursor(self) -> FakeCursor:
        return self.cursor_obj


def test_resolve_alias_returns_ticker_when_found(monkeypatch) -> None:
    connection = FakeConnection(("AAPL",))

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(reads, "get_connection", fake_get_connection)

    result = reads.resolve_alias("Apple Inc.")

    assert result == "AAPL"
    sql_text, params = connection.cursor_obj.executed[0]
    assert "ticker_aliases" in sql_text
    assert params == {"alias": "apple inc."}


def test_resolve_alias_returns_none_when_not_found(monkeypatch) -> None:
    connection = FakeConnection(None)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(reads, "get_connection", fake_get_connection)

    assert reads.resolve_alias("Unknown Co") is None
