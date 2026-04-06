"""Typed read helpers for DB1 — `market`.

Every function returns a pandas DataFrame so that downstream feature engineering
and dashboards stay compatible with the existing MSARN code paths.

Frozen API surface (do not change without bumping research_commons version):

    load_prices(ticker, *, start=None, end=None) -> pd.DataFrame
    load_prices_many(tickers, *, start=None, end=None) -> pd.DataFrame
    load_features(ticker, *, start=None, end=None) -> pd.DataFrame
    load_fundamentals(ticker) -> pd.DataFrame
    load_macro(*, start=None, end=None) -> pd.DataFrame
    load_cross_asset(*, start=None, end=None) -> pd.DataFrame
    load_ticker_metadata() -> pd.DataFrame
    load_backtest_results(model_id) -> pd.DataFrame
"""

from __future__ import annotations

from datetime import date

import pandas as pd


def load_prices(
    ticker: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return OHLCV + currency for one ticker, indexed by date."""
    raise NotImplementedError("Stub. SELECT * FROM prices WHERE ticker = %s …")


def load_prices_many(
    tickers: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return long-format OHLCV for multiple tickers in a single query."""
    raise NotImplementedError("Stub. SELECT … WHERE ticker = ANY(%s) …")


def load_features(
    ticker: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return the engineered feature matrix for one ticker."""
    raise NotImplementedError("Stub. SELECT * FROM features WHERE ticker = %s …")


def load_fundamentals(ticker: str) -> pd.DataFrame:
    """Return all fundamentals snapshots for one ticker, sorted by as_of."""
    raise NotImplementedError("Stub.")


def load_macro(
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return wide-format macro time series (VIX, yields, spreads…)."""
    raise NotImplementedError("Stub.")


def load_cross_asset(
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return long-format cross-asset returns (SPY, TLT, GLD, VIX, ACWI, sectors)."""
    raise NotImplementedError("Stub.")


def load_ticker_metadata() -> pd.DataFrame:
    """Return the entire `ticker_metadata` table."""
    raise NotImplementedError("Stub. SELECT * FROM ticker_metadata.")


def load_backtest_results(model_id: int) -> pd.DataFrame:
    """Return walk-forward backtest rows for one model registry id."""
    raise NotImplementedError("Stub.")
