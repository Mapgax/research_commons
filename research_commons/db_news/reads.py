"""Typed read helpers for DB2 — `news`.

Frozen API surface:

    load_articles(*, since=None, source=None, ticker=None, limit=None) -> pd.DataFrame
    load_article(article_id) -> dict
    load_classifications(*, classifier_version=None, since=None) -> pd.DataFrame
    load_sentiment_daily(ticker, *, start=None, end=None) -> pd.DataFrame
    load_sentiment_daily_many(tickers, *, start=None, end=None) -> pd.DataFrame
    load_briefings(*, limit=10) -> pd.DataFrame
    load_documents(*, source_pipeline=None, since=None) -> pd.DataFrame
    load_pipeline_runs(pipeline_name, *, limit=20) -> pd.DataFrame
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

from research_commons.db_news.connection import get_connection

logger = logging.getLogger(__name__)


def load_articles(
    *,
    since: datetime | None = None,
    source: str | None = None,
    ticker: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Return raw articles, optionally filtered."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if since is not None:
        clauses.append("a.published_at >= %(since)s")
        params["since"] = since
    if source is not None:
        clauses.append("a.source = %(source)s")
        params["source"] = source

    join = ""
    if ticker is not None:
        join = "JOIN article_companies ac ON ac.article_id = a.id"
        clauses.append("ac.ticker = %(ticker)s")
        params["ticker"] = ticker

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit_clause = f"LIMIT {int(limit)}" if limit else ""

    sql = f"""
        SELECT a.*
        FROM articles a
        {join}
        {where}
        ORDER BY a.published_at DESC NULLS LAST
        {limit_clause}
    """
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["published_at", "fetched_at"])


def load_article(article_id: int) -> dict[str, Any]:
    """Return one article row as a dict (including its raw JSONB payload)."""
    sql = "SELECT * FROM articles WHERE id = %s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (article_id,))
            cols = [desc[0] for desc in cur.description]
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"No article with id={article_id}")
            return dict(zip(cols, row))


def load_classifications(
    *,
    classifier_version: str | None = None,
    since: datetime | None = None,
) -> pd.DataFrame:
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if classifier_version is not None:
        clauses.append("classifier_version = %(cv)s")
        params["cv"] = classifier_version
    if since is not None:
        clauses.append("classified_at >= %(since)s")
        params["since"] = since

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM article_classifications {where} ORDER BY classified_at DESC"

    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["classified_at"])


def load_sentiment_daily(
    ticker: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return daily sentiment aggregates for one ticker.

    This is the bridge that ``features/engineering.py`` joins into the market
    feature matrix in pandas (since cross-DB SQL joins are impossible).
    """
    clauses = ["ticker = %(ticker)s"]
    params: dict[str, Any] = {"ticker": ticker}

    if start is not None:
        clauses.append("date >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append("date <= %(end)s")
        params["end"] = end

    where = "WHERE " + " AND ".join(clauses)
    sql = f"""
        SELECT date, n_articles, sentiment_mean, sentiment_std,
               severity_max, classifier_version
        FROM sentiment_daily
        {where}
        ORDER BY date
    """
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])


def load_sentiment_daily_many(
    tickers: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return daily sentiment for multiple tickers in a single query."""
    if not tickers:
        return pd.DataFrame()

    clauses = ["ticker = ANY(%(tickers)s)"]
    params: dict[str, Any] = {"tickers": tickers}

    if start is not None:
        clauses.append("date >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append("date <= %(end)s")
        params["end"] = end

    where = "WHERE " + " AND ".join(clauses)
    sql = f"""
        SELECT ticker, date, n_articles, sentiment_mean, sentiment_std,
               severity_max, classifier_version
        FROM sentiment_daily
        {where}
        ORDER BY ticker, date
    """
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])


def load_briefings(*, limit: int = 10) -> pd.DataFrame:
    sql = """
        SELECT * FROM briefings
        ORDER BY as_of DESC
        LIMIT %(limit)s
    """
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params={"limit": limit})


def load_documents(
    *,
    source_pipeline: str | None = None,
    since: datetime | None = None,
) -> pd.DataFrame:
    """Read the unified `documents` table."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if source_pipeline is not None:
        clauses.append("source_pipeline = %(sp)s")
        params["sp"] = source_pipeline
    if since is not None:
        clauses.append("inserted_at >= %(since)s")
        params["since"] = since

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM documents {where} ORDER BY inserted_at DESC"

    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["inserted_at"])


def load_pipeline_runs(pipeline_name: str, *, limit: int = 20) -> pd.DataFrame:
    sql = """
        SELECT * FROM pipeline_runs
        WHERE pipeline_name = %(pn)s
        ORDER BY started_at DESC
        LIMIT %(limit)s
    """
    with get_connection() as conn:
        return pd.read_sql_query(
            sql, conn,
            params={"pn": pipeline_name, "limit": limit},
            parse_dates=["started_at", "finished_at"],
        )
