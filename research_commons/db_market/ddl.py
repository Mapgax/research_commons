"""Python view of the DB1 schema.

This module exists for **two** reasons:

1. To give CI/tests a Python-importable list of expected tables that can be
   compared against ``information_schema.tables`` at startup.
2. To document the natural keys that ``writes.py`` relies on for upserts.

The actual DDL lives in ``migrations/market/0001_init.sql``. Treat THIS file as
read-only metadata; never use it as a substitute for the real migration.
"""

from __future__ import annotations

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
