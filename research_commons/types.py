"""Shared row / payload types passed between projects.

Kept as TypedDicts (cheap, JSON-serialisable) rather than ORM models to avoid
forcing every project onto SQLAlchemy.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, TypedDict


# ----- DB1: market -------------------------------------------------------------

class PriceRow(TypedDict):
    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: float
    currency: str  # ISO 4217


class FundamentalRow(TypedDict, total=False):
    ticker: str
    as_of: date
    trailing_pe: float | None
    forward_pe: float | None
    price_to_book: float | None
    debt_to_equity: float | None
    return_on_equity: float | None
    profit_margins: float | None
    ev_to_ebitda: float | None


# ----- DB2: news ---------------------------------------------------------------

ArticleSource = Literal[
    "newsapi", "gdelt", "finnhub", "google_news_rss",
    "edgar", "manual", "scraper", "other",
]


class ArticleRow(TypedDict):
    source: ArticleSource
    source_article_id: str | None  # provider-side primary key, if any
    url: str
    canonical_url: str             # post-normalize
    content_hash: str              # SHA-256 of canonical_url + title + body
    title: str
    body: str | None
    published_at: datetime | None
    fetched_at: datetime
    language: str | None           # ISO 639-1
    raw: dict | None               # provider payload, JSONB


class ClassificationRow(TypedDict, total=False):
    article_id: int
    classifier_version: str        # e.g. "claude-haiku-4-5-2026-04-06"
    event_type: str | None
    severity: int | None           # 1..5
    sentiment_score: float | None  # -1..+1
    summary: str | None
    tickers: list[str]             # normalized ecosystem tickers
    themes: list[str]
    raw: dict | None
