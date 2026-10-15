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

    def executemany(self, sql_text: str, seq: list[object]) -> None:
        for params in seq:
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

    count = writes.refresh_sentiment_daily(relevance_threshold=0.5)

    assert count == 3
    assert connection.committed is True

    sql_text, params = connection.cursor_obj.executed[0]
    assert "source_count" in sql_text
    assert "source_tiers_present" in sql_text
    assert "JOIN sources s ON s.name = a.source" in sql_text
    assert "count(DISTINCT a.source)" in sql_text
    assert "array_agg(DISTINCT s.reliability_tier)" in sql_text
    assert params == {"rel_threshold": 0.5}


def test_refresh_sentiment_daily_since_filter_passes_through(monkeypatch) -> None:
    from datetime import date

    connection = FakeConnection(rowcount=1)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    cutoff = date(2026, 1, 1)
    count = writes.refresh_sentiment_daily(since=cutoff, relevance_threshold=0.5)

    assert count == 1
    sql_text, params = connection.cursor_obj.executed[0]
    assert "a.published_at >= %(since)s" in sql_text
    assert params == {"since": cutoff, "rel_threshold": 0.5}


def _normalize(sql_text: str) -> str:
    return " ".join(sql_text.split())


def test_refresh_sentiment_daily_keeps_existing_column_definitions(monkeypatch) -> None:
    # Consumers read these via @main; the weighted columns must be additive.
    connection = FakeConnection(rowcount=1)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    writes.refresh_sentiment_daily(relevance_threshold=0.5)

    sql_text = _normalize(connection.cursor_obj.executed[0][0])
    for expr in (
        "count(*) AS n_articles,",
        "avg(c.sentiment_score) AS sentiment_mean,",
        "stddev_samp(c.sentiment_score) AS sentiment_std,",
        "max(c.severity) AS severity_max,",
        "max(c.classifier_version) AS classifier_version,",
        "n_articles = EXCLUDED.n_articles,",
        "sentiment_mean = EXCLUDED.sentiment_mean,",
        "severity_max = EXCLUDED.severity_max,",
    ):
        assert expr in sql_text, expr
    # Existing columns aggregate every link: no relevance filter in the WHERE.
    where = sql_text.split(" WHERE a.published_at IS NOT NULL", 1)[1].split("GROUP BY")[0]
    assert "relevance" not in where


def test_refresh_sentiment_daily_weights_by_effective_relevance(monkeypatch) -> None:
    connection = FakeConnection(rowcount=1)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    writes.refresh_sentiment_daily(relevance_threshold=0.7)

    sql_text, params = connection.cursor_obj.executed[0]
    sql_text = _normalize(sql_text)
    eff = "COALESCE(ac.relevance_judged, ac.relevance)"
    relevant = f"{eff} >= %(rel_threshold)s"
    assert params["rel_threshold"] == 0.7
    assert f"count(*) FILTER (WHERE {relevant}) AS n_articles_relevant" in sql_text
    assert f"sum({eff} * c.sentiment_score) FILTER (WHERE {relevant})" in sql_text
    assert (
        f"NULLIF(sum({eff}) FILTER ( WHERE {relevant} AND c.sentiment_score IS NOT NULL), 0)"
        in sql_text
    )
    assert f"max(c.severity) FILTER (WHERE {relevant}) AS severity_max_relevant" in sql_text
    assert "COALESCE(ac.relevance_judge, 'prior')" in sql_text
    for col in ("n_articles_relevant", "sentiment_mean_weighted",
                "severity_max_relevant", "relevance_judge"):
        assert f"{col} = EXCLUDED.{col}" in sql_text


def test_upsert_article_companies_never_touches_judged_columns(monkeypatch) -> None:
    # A re-sync must not erase a paid judgment.
    connection = FakeConnection(rowcount=1)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    writes.upsert_article_companies(42, ["AAPL"], relevance={"AAPL": 1.0})

    sql_text, _ = connection.cursor_obj.executed[0]
    assert "relevance_judged" not in sql_text
    assert "relevance_judge" not in sql_text


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


def test_upsert_article_companies_is_additive(monkeypatch) -> None:
    # Callers sync one ticker at a time; a delete-then-insert kept only the
    # last ticker an article was fetched under.
    connection = FakeConnection(rowcount=1)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    writes.upsert_article_companies(42, ["AAPL"], relevance={"AAPL": 1.0})
    writes.upsert_article_companies(42, ["MSFT"], relevance={"MSFT": 0.3})

    executed = connection.cursor_obj.executed
    assert not any("DELETE" in sql.upper() for sql, _ in executed)
    assert [params for _, params in executed] == [(42, "AAPL", 1.0), (42, "MSFT", 0.3)]
    assert "ON CONFLICT (article_id, ticker) DO UPDATE" in executed[0][0]
    assert connection.committed is True


def test_upsert_article_companies_keeps_existing_score_without_relevance(monkeypatch) -> None:
    connection = FakeConnection(rowcount=1)

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr(writes, "get_connection", fake_get_connection)

    writes.upsert_article_companies(7, ["NVDA"])

    sql_text, params = connection.cursor_obj.executed[0]
    assert params == (7, "NVDA", None)
    assert "COALESCE(EXCLUDED.relevance, article_companies.relevance)" in sql_text


def test_upsert_article_companies_empty_tickers_touches_nothing(monkeypatch) -> None:
    def fail_get_connection():
        raise AssertionError("no connection expected for an empty ticker list")

    monkeypatch.setattr(writes, "get_connection", fail_get_connection)

    writes.upsert_article_companies(7, [])
