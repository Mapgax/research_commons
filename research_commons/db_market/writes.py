"""Typed write helpers for DB1 — `market`.

All writes are idempotent: ON CONFLICT DO UPDATE on the natural key. This makes
it safe to re-run incremental jobs without duplication.

Frozen API surface:

    upsert_prices(rows: list[PriceRow]) -> int
    upsert_fundamentals(rows: list[FundamentalRow]) -> int
    upsert_features(df: pd.DataFrame) -> int
    upsert_macro(rows: list[dict]) -> int
    upsert_cross_asset(rows: list[dict]) -> int
    register_model(...) -> int
    record_backtest_result(...) -> int
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from research_commons.types import FundamentalRow, PriceRow


def upsert_prices(rows: list[PriceRow]) -> int:
    """Insert or update OHLCV rows. Returns number of rows affected."""
    raise NotImplementedError(
        "Stub. INSERT INTO prices ... ON CONFLICT (ticker, date) DO UPDATE …"
    )


def upsert_fundamentals(rows: list[FundamentalRow]) -> int:
    raise NotImplementedError(
        "Stub. ON CONFLICT (ticker, as_of) DO UPDATE …"
    )


def upsert_features(df: pd.DataFrame) -> int:
    """Bulk-upsert the engineered feature matrix.

    The DataFrame must have ``ticker`` and ``date`` columns plus all feature
    columns listed in ``models.trainer.FEATURE_COLS``.
    """
    raise NotImplementedError(
        "Stub. Use psycopg2.extras.execute_values for batched UPSERT."
    )


def upsert_macro(rows: list[dict[str, Any]]) -> int:
    raise NotImplementedError("Stub. ON CONFLICT (date) DO UPDATE …")


def upsert_cross_asset(rows: list[dict[str, Any]]) -> int:
    raise NotImplementedError("Stub. ON CONFLICT (asset, date) DO UPDATE …")


def register_model(
    *,
    ticker: str,
    target: str,
    architecture: str,
    checkpoint_path: str,
    metrics: dict[str, Any],
    config: dict[str, Any],
) -> int:
    """Insert a row into ``model_registry`` and return its id."""
    raise NotImplementedError("Stub. INSERT … RETURNING id.")


def record_backtest_result(
    *,
    model_id: int,
    rows: list[dict[str, Any]],
) -> int:
    """Append walk-forward backtest rows for a given model id."""
    raise NotImplementedError("Stub.")
