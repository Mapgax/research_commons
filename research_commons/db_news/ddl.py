"""Python view of the DB2 schema. See db_market/ddl.py for rationale."""

from __future__ import annotations

EXPECTED_TABLES: tuple[str, ...] = (
    "sources",
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
