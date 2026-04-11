"""Python view of the DB2 schema.

This module still exposes importable metadata (`EXPECTED_TABLES`, `NATURAL_KEYS`)
for tests and callers, but it now also provides a minimal bootstrap helper for
local initialization. The actual SQL remains in ``migrations/news/*.sql`` so the
schema stays defined in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import psycopg2.extensions

EXPECTED_TABLES: tuple[str, ...] = (
    "sources",
    "source_health",
    "articles",
    "article_classifications",
    "article_companies",
    "sentiment_daily",
    "trend_snapshots",
    "documents",
    "document_items",
    "briefings",
    "briefing_items",
    "pipeline_runs",
)

NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "sources":                 ("name",),
    "source_health":           ("id",),
    "articles":                ("content_hash",),
    "article_classifications": ("article_id", "classifier_version"),
    "article_companies":       ("article_id", "ticker"),
    "sentiment_daily":         ("ticker", "date"),
    "trend_snapshots":         ("theme", "date"),
    "documents":               ("source_pipeline", "external_id"),
    "document_items":          ("document_id", "ordinal"),
    "briefings":               ("as_of", "audience"),
    "briefing_items":          ("briefing_id", "ordinal"),
    "pipeline_runs":           ("id",),
}


def _migration_paths() -> Iterable[Path]:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "news"
    return sorted(migrations_dir.glob("*.sql"))


def create_tables(conn: psycopg2.extensions.connection) -> None:
    """Apply the bundled news DB migrations in filename order.

    What you can learn from this design:
    - Schema ownership stays in SQL, which keeps Python free of duplicated DDL.
    - Idempotence is delegated to the migrations themselves via
      ``CREATE ... IF NOT EXISTS`` / ``ALTER ... IF NOT EXISTS``.
    - The helper is intentionally small: one connection in, migrations applied,
      commit on success, rollback on failure.
    """
    migration_paths = tuple(_migration_paths())
    if not migration_paths:
        raise FileNotFoundError("No news DB migration files were found.")

    try:
        with conn.cursor() as cur:
            for migration_path in migration_paths:
                cur.execute(migration_path.read_text(encoding="utf-8"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
