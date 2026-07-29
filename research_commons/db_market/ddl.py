"""Python view of the DB1 schema.

This module exposes importable metadata (`EXPECTED_TABLES`, `NATURAL_KEYS`) for
tests and callers, plus a minimal bootstrap helper mirroring the news-side
``db_news.ddl.create_tables``. The actual SQL lives in
``migrations/market/*.sql`` so the schema stays defined in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import psycopg2.extensions

EXPECTED_TABLES: tuple[str, ...] = (
    "prices",
    "fundamentals",
    "ticker_metadata",
    "sector_etf_registry",
    "macro_data",
    "cross_asset",
    "insider_transactions",
    "option_metrics",
    "alt_data",
    "features",
    "model_registry",
    "model_benchmarks",
    "regime_models",
    "rl_portfolio",
    "backtest_results",
    "scheduler_leases",
    "scheduler_runs",
    "app_state",
)

# Natural keys used by ON CONFLICT clauses in writes.py.
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "prices":               ("ticker", "date"),
    "fundamentals":         ("ticker", "as_of"),
    "ticker_metadata":      ("ticker",),
    "sector_etf_registry":  ("sector",),
    "macro_data":           ("date",),
    "cross_asset":          ("asset", "date"),
    "insider_transactions": ("ticker", "filing_url"),
    "option_metrics":       ("ticker", "date"),
    "alt_data":             ("ticker", "date", "source"),
    "features":             ("ticker", "date"),
    "model_registry":       ("id",),
    "model_benchmarks":     ("id",),
    "regime_models":        ("id",),
    "rl_portfolio":         ("id",),
    "backtest_results":     ("model_id", "date"),
    "scheduler_leases":     ("name",),
    "scheduler_runs":       ("id",),
    "app_state":            ("key",),
}


def _migration_paths() -> Iterable[Path]:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "market"
    return sorted(migrations_dir.glob("*.sql"))


def create_tables(conn: psycopg2.extensions.connection) -> None:
    """Apply the bundled market DB migrations in filename order.

    Same design as ``db_news.ddl.create_tables``: schema ownership stays in
    SQL, idempotence is delegated to the migrations themselves, one connection
    in, commit on success, rollback on failure.
    """
    migration_paths = tuple(_migration_paths())
    if not migration_paths:
        raise FileNotFoundError("No market DB migration files were found.")

    try:
        with conn.cursor() as cur:
            for migration_path in migration_paths:
                cur.execute(migration_path.read_text(encoding="utf-8"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
