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

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from research_commons.db_news.connection import get_connection
from research_commons.types import ArticleRow, ClassificationRow

logger = logging.getLogger(__name__)


def upsert_article(payload: ArticleRow) -> int:
    """Insert a raw article. Returns the (existing or new) ``article_id``.

    Deduplication is based on ``content_hash`` (SHA-256 of canonical_url +
    title + body) — see ``research_commons.sources.normalize.content_hash``.
    """
    sql = """
        INSERT INTO articles
            (source, source_article_id, url, canonical_url, content_hash,
             title, body, published_at, fetched_at, language, raw)
        VALUES
            (%(source)s, %(source_article_id)s, %(url)s, %(canonical_url)s,
             %(content_hash)s, %(title)s, %(body)s, %(published_at)s,
             %(fetched_at)s, %(language)s, %(raw)s::jsonb)
        ON CONFLICT (content_hash) DO UPDATE SET
            fetched_at = EXCLUDED.fetched_at
        RETURNING id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "source": payload["source"],
                "source_article_id": payload.get("source_article_id"),
                "url": payload["url"],
                "canonical_url": payload["canonical_url"],
                "content_hash": payload["content_hash"],
                "title": payload["title"],
                "body": payload.get("body"),
                "published_at": payload.get("published_at"),
                "fetched_at": payload.get("fetched_at", datetime.now(timezone.utc)),
                "language": payload.get("language"),
                "raw": json.dumps(payload.get("raw") or {}),
            })
            row = cur.fetchone()
            conn.commit()
    return row[0]


def upsert_articles_bulk(payloads: list[ArticleRow]) -> list[int]:
    """Bulk variant of :func:`upsert_article`. Order of returned ids matches input."""
    if not payloads:
        return []

    sql = """
        INSERT INTO articles
            (source, source_article_id, url, canonical_url, content_hash,
             title, body, published_at, fetched_at, language, raw)
        VALUES
            (%(source)s, %(source_article_id)s, %(url)s, %(canonical_url)s,
             %(content_hash)s, %(title)s, %(body)s, %(published_at)s,
             %(fetched_at)s, %(language)s, %(raw)s::jsonb)
        ON CONFLICT (content_hash) DO UPDATE SET
            fetched_at = EXCLUDED.fetched_at
        RETURNING id
    """
    ids: list[int] = []
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            for p in payloads:
                cur.execute(sql, {
                    "source": p["source"],
                    "source_article_id": p.get("source_article_id"),
                    "url": p["url"],
                    "canonical_url": p["canonical_url"],
                    "content_hash": p["content_hash"],
                    "title": p["title"],
                    "body": p.get("body"),
                    "published_at": p.get("published_at"),
                    "fetched_at": p.get("fetched_at", now),
                    "language": p.get("language"),
                    "raw": json.dumps(p.get("raw") or {}),
                })
                ids.append(cur.fetchone()[0])
            conn.commit()
    return ids


def upsert_classification(
    article_id: int,
    payload: ClassificationRow,
) -> int:
    """Insert (or replace) the classification for one article + classifier_version.

    The natural key is (article_id, classifier_version), which means rerunning
    a newer classifier version creates a *new* row rather than overwriting
    history — important for reproducibility of historical features.
    """
    sql = """
        INSERT INTO article_classifications
            (article_id, classifier_version, event_type, severity,
             sentiment_score, summary, raw)
        VALUES
            (%(article_id)s, %(classifier_version)s, %(event_type)s,
             %(severity)s, %(sentiment_score)s, %(summary)s, %(raw)s::jsonb)
        ON CONFLICT (article_id, classifier_version) DO UPDATE SET
            event_type      = EXCLUDED.event_type,
            severity        = EXCLUDED.severity,
            sentiment_score = EXCLUDED.sentiment_score,
            summary         = EXCLUDED.summary,
            raw             = EXCLUDED.raw,
            classified_at   = now()
        RETURNING article_id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "article_id": article_id,
                "classifier_version": payload.get("classifier_version", "unknown"),
                "event_type": payload.get("event_type"),
                "severity": payload.get("severity"),
                "sentiment_score": payload.get("sentiment_score"),
                "summary": payload.get("summary"),
                "raw": json.dumps(payload.get("raw") or {}),
            })
            row = cur.fetchone()
            conn.commit()
    return row[0]


def upsert_article_companies(article_id: int, tickers: list[str]) -> None:
    """Replace the (article_id -> ticker) mapping for one article."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM article_companies WHERE article_id = %s",
                (article_id,),
            )
            if tickers:
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    "INSERT INTO article_companies (article_id, ticker) VALUES %s "
                    "ON CONFLICT (article_id, ticker) DO NOTHING",
                    [(article_id, t) for t in tickers],
                )
            conn.commit()


def refresh_sentiment_daily(*, since: date | None = None) -> int:
    """Recompute the ``sentiment_daily`` aggregate.

    Uses the latest classifier_version per article. Returns number of
    (ticker, date) rows touched.
    """
    where_clause = ""
    params: dict[str, Any] = {}
    if since is not None:
        where_clause = "AND a.published_at >= %(since)s"
        params["since"] = since

    sql = f"""
        INSERT INTO sentiment_daily
            (ticker, date, n_articles, sentiment_mean, sentiment_std,
             severity_max, classifier_version, refreshed_at)
        SELECT
            ac.ticker,
            a.published_at::date            AS date,
            count(*)                        AS n_articles,
            avg(c.sentiment_score)          AS sentiment_mean,
            stddev_samp(c.sentiment_score)  AS sentiment_std,
            max(c.severity)                 AS severity_max,
            max(c.classifier_version)       AS classifier_version,
            now()                           AS refreshed_at
        FROM articles a
        JOIN article_companies ac ON ac.article_id = a.id
        JOIN LATERAL (
            SELECT *
            FROM article_classifications
            WHERE article_id = a.id
            ORDER BY classified_at DESC
            LIMIT 1
        ) c ON true
        WHERE a.published_at IS NOT NULL
          {where_clause}
        GROUP BY ac.ticker, a.published_at::date
        ON CONFLICT (ticker, date) DO UPDATE SET
            n_articles        = EXCLUDED.n_articles,
            sentiment_mean    = EXCLUDED.sentiment_mean,
            sentiment_std     = EXCLUDED.sentiment_std,
            severity_max      = EXCLUDED.severity_max,
            classifier_version = EXCLUDED.classifier_version,
            refreshed_at      = EXCLUDED.refreshed_at
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            count = cur.rowcount
            conn.commit()
    logger.info("refresh_sentiment_daily: %d rows upserted", count)
    return count


def upsert_briefing(
    *,
    as_of: date,
    audience: str,
    html_body: str,
    payload: dict[str, Any],
) -> int:
    """Insert or update a briefing record. Returns the briefing id."""
    sql = """
        INSERT INTO briefings (as_of, audience, html_body, payload, delivered_at)
        VALUES (%(as_of)s, %(audience)s, %(html_body)s, %(payload)s::jsonb, now())
        ON CONFLICT (as_of, audience) DO UPDATE SET
            html_body    = EXCLUDED.html_body,
            payload      = EXCLUDED.payload,
            delivered_at = EXCLUDED.delivered_at
        RETURNING id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "as_of": as_of,
                "audience": audience,
                "html_body": html_body,
                "payload": json.dumps(payload),
            })
            row = cur.fetchone()
            conn.commit()
    return row[0]


def upsert_document(
    *,
    source_pipeline: str,
    external_id: str,
    title: str,
    body: str,
    metadata: dict[str, Any],
    items: list[dict[str, Any]] | None = None,
) -> int:
    """Insert into the unified ``documents`` table (+ optional document_items).

    ``source_pipeline`` discriminates legacy paths: ``"email_research"``
    (MSARN) and ``"manual_research"`` (Companies_News).
    """
    sql = """
        INSERT INTO documents (source_pipeline, external_id, title, body, metadata)
        VALUES (%(source_pipeline)s, %(external_id)s, %(title)s, %(body)s, %(metadata)s::jsonb)
        ON CONFLICT (source_pipeline, external_id) DO UPDATE SET
            title    = EXCLUDED.title,
            body     = EXCLUDED.body,
            metadata = EXCLUDED.metadata
        RETURNING id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "source_pipeline": source_pipeline,
                "external_id": external_id,
                "title": title,
                "body": body,
                "metadata": json.dumps(metadata),
            })
            doc_id = cur.fetchone()[0]

            if items:
                cur.execute(
                    "DELETE FROM document_items WHERE document_id = %s",
                    (doc_id,),
                )
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    "INSERT INTO document_items "
                    "(document_id, ordinal, item_type, content, metadata) "
                    "VALUES %s",
                    [
                        (doc_id, i, it.get("item_type", "paragraph"),
                         it["content"], json.dumps(it.get("metadata", {})))
                        for i, it in enumerate(items)
                    ],
                )
            conn.commit()
    return doc_id


def record_pipeline_run(
    *,
    pipeline_name: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    status: str,
    rows_in: int = 0,
    rows_out: int = 0,
    error: str | None = None,
    extras: dict[str, Any] | None = None,
) -> int:
    """Append a row to ``pipeline_runs``. Returns the new id."""
    sql = """
        INSERT INTO pipeline_runs
            (pipeline_name, started_at, finished_at, status,
             rows_in, rows_out, error, extras)
        VALUES
            (%(pipeline_name)s, %(started_at)s, %(finished_at)s,
             %(status)s, %(rows_in)s, %(rows_out)s, %(error)s,
             %(extras)s::jsonb)
        RETURNING id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "pipeline_name": pipeline_name,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": status,
                "rows_in": rows_in,
                "rows_out": rows_out,
                "error": error,
                "extras": json.dumps(extras or {}),
            })
            row = cur.fetchone()
            conn.commit()
    return row[0]
