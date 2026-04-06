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

from datetime import date, datetime
from typing import Any

import pandas as pd


def load_articles(
    *,
    since: datetime | None = None,
    source: str | None = None,
    ticker: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Return raw articles, optionally filtered."""
    raise NotImplementedError(
        "Stub. SELECT a.* FROM articles a [JOIN article_companies …] WHERE …"
    )


def load_article(article_id: int) -> dict[str, Any]:
    """Return one article row as a dict (including its raw JSONB payload)."""
    raise NotImplementedError("Stub.")


def load_classifications(
    *,
    classifier_version: str | None = None,
    since: datetime | None = None,
) -> pd.DataFrame:
    raise NotImplementedError("Stub.")


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
    raise NotImplementedError("Stub. SELECT … FROM sentiment_daily WHERE ticker = %s …")


def load_sentiment_daily_many(
    tickers: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    raise NotImplementedError("Stub.")


def load_briefings(*, limit: int = 10) -> pd.DataFrame:
    raise NotImplementedError("Stub. SELECT * FROM briefings ORDER BY as_of DESC LIMIT …")


def load_documents(
    *,
    source_pipeline: str | None = None,
    since: datetime | None = None,
) -> pd.DataFrame:
    """Read the unified `documents` table.

    `source_pipeline` is the discriminator that distinguishes the legacy
    `email_research_*` (MSARN) and `manual_research_*` (Companies_News) rows.
    """
    raise NotImplementedError("Stub.")


def load_pipeline_runs(pipeline_name: str, *, limit: int = 20) -> pd.DataFrame:
    raise NotImplementedError("Stub.")
