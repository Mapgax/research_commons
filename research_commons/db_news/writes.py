"""Typed write helpers for DB2 — `news`.

All writes are idempotent: ``ON CONFLICT (content_hash) DO NOTHING`` for
articles, ``ON CONFLICT … DO UPDATE`` for derived rows.

Frozen API surface:

    upsert_article(payload: ArticleRow) -> int
    upsert_articles_bulk(payloads: list[ArticleRow]) -> list[int]
    upsert_classification(article_id: int, payload: ClassificationRow) -> int
    upsert_article_companies(article_id: int, tickers: list[str]) -> None
    refresh_sentiment_daily(*, since: date | None = None) -> int
    upsert_briefing(...) -> int
    upsert_document(...) -> int
    record_pipeline_run(...) -> int
"""

from __future__ import annotations

from datetime import date
from typing import Any

from research_commons.types import ArticleRow, ClassificationRow


def upsert_article(payload: ArticleRow) -> int:
    """Insert a raw article. Returns the (existing or new) ``article_id``.

    Deduplication is based on ``content_hash`` (SHA-256 of canonical_url +
    title + body) — see ``research_commons.sources.normalize.content_hash``.
    """
    raise NotImplementedError(
        "Stub. INSERT … ON CONFLICT (content_hash) DO UPDATE SET fetched_at = … "
        "RETURNING id."
    )


def upsert_articles_bulk(payloads: list[ArticleRow]) -> list[int]:
    """Bulk variant of :func:`upsert_article`. Order of returned ids matches input."""
    raise NotImplementedError("Stub. execute_values + RETURNING id.")


def upsert_classification(
    article_id: int,
    payload: ClassificationRow,
) -> int:
    """Insert (or replace) the classification for one article + classifier_version.

    The natural key is (article_id, classifier_version), which means rerunning
    a newer classifier version creates a *new* row rather than overwriting
    history — important for reproducibility of historical features.
    """
    raise NotImplementedError(
        "Stub. ON CONFLICT (article_id, classifier_version) DO UPDATE …"
    )


def upsert_article_companies(article_id: int, tickers: list[str]) -> None:
    """Replace the (article_id → ticker) mapping for one article."""
    raise NotImplementedError("Stub. DELETE then INSERT in one transaction.")


def refresh_sentiment_daily(*, since: date | None = None) -> int:
    """Recompute the ``sentiment_daily`` aggregate.

    Should be cheap enough to run nightly. Uses the latest classifier_version
    per article. Returns number of (ticker, date) rows touched.
    """
    raise NotImplementedError(
        "Stub. INSERT INTO sentiment_daily SELECT … FROM articles a JOIN "
        "article_companies ac … JOIN article_classifications c … "
        "ON CONFLICT (ticker, date) DO UPDATE …"
    )


def upsert_briefing(
    *,
    as_of: date,
    audience: str,
    html_body: str,
    payload: dict[str, Any],
) -> int:
    raise NotImplementedError("Stub.")


def upsert_document(
    *,
    source_pipeline: str,
    title: str,
    body: str,
    metadata: dict[str, Any],
    items: list[dict[str, Any]] | None = None,
) -> int:
    """Insert into the unified ``documents`` table (+ optional document_items).

    ``source_pipeline`` discriminates legacy paths: ``"email_research"``
    (MSARN) and ``"manual_research"`` (Companies_News).
    """
    raise NotImplementedError("Stub.")


def record_pipeline_run(
    *,
    pipeline_name: str,
    started_at,
    finished_at,
    status: str,
    rows_in: int,
    rows_out: int,
    error: str | None = None,
    extras: dict[str, Any] | None = None,
) -> int:
    """Append a row to ``pipeline_runs``. Returns the new id."""
    raise NotImplementedError("Stub.")
