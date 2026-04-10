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
    load_alt_data(ticker, *, start=None, end=None) -> pd.DataFrame
    load_option_metrics(ticker, *, start=None, end=None) -> pd.DataFrame
    load_ticker_metadata() -> pd.DataFrame
    load_backtest_results(model_id) -> pd.DataFrame
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd

from research_commons.db_market.connection import get_connection

logger = logging.getLogger(__name__)


def _date_clauses(
    start: date | None,
    end: date | None,
    col: str = "date",
) -> tuple[str, dict[str, Any]]:
    """Build WHERE fragments for date range filtering."""
    parts: list[str] = []
    params: dict[str, Any] = {}
    if start is not None:
        parts.append(f"{col} >= %(start)s")
        params["start"] = start
    if end is not None:
        parts.append(f"{col} <= %(end)s")
        params["end"] = end
    return (" AND ".join(parts), params) if parts else ("", {})


def load_prices(
    ticker: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return OHLCV + currency for one ticker, indexed by date."""
    date_frag, params = _date_clauses(start, end)
    params["ticker"] = ticker
    where = f"WHERE ticker = %(ticker)s" + (f" AND {date_frag}" if date_frag else "")

    sql = f"SELECT * FROM prices {where} ORDER BY date"
    with get_connection() as conn:
        df = pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])
    if not df.empty:
        df = df.set_index("date")
    return df


def load_prices_many(
    tickers: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return long-format OHLCV for multiple tickers in a single query."""
    if not tickers:
        return pd.DataFrame()

    date_frag, params = _date_clauses(start, end)
    params["tickers"] = tickers
    where = "WHERE ticker = ANY(%(tickers)s)" + (f" AND {date_frag}" if date_frag else "")

    sql = f"SELECT * FROM prices {where} ORDER BY ticker, date"
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])


def load_features(
    ticker: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return the engineered feature matrix for one ticker."""
    date_frag, params = _date_clauses(start, end)
    params["ticker"] = ticker
    where = f"WHERE ticker = %(ticker)s" + (f" AND {date_frag}" if date_frag else "")

    sql = f"SELECT * FROM features {where} ORDER BY date"
    with get_connection() as conn:
        df = pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])
    if not df.empty:
        df = df.set_index("date")
    return df


def load_fundamentals(ticker: str) -> pd.DataFrame:
    """Return all fundamentals snapshots for one ticker, sorted by as_of."""
    sql = "SELECT * FROM fundamentals WHERE ticker = %(ticker)s ORDER BY as_of"
    with get_connection() as conn:
        return pd.read_sql_query(
            sql, conn, params={"ticker": ticker}, parse_dates=["as_of"],
        )


def load_macro(
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return wide-format macro time series (VIX, yields, spreads)."""
    date_frag, params = _date_clauses(start, end)
    where = f"WHERE {date_frag}" if date_frag else ""

    sql = f"SELECT * FROM macro_data {where} ORDER BY date"
    with get_connection() as conn:
        df = pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])
    if not df.empty:
        df = df.set_index("date")
    return df


def load_cross_asset(
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return long-format cross-asset returns (SPY, TLT, GLD, VIX, ACWI, sectors)."""
    date_frag, params = _date_clauses(start, end)
    where = f"WHERE {date_frag}" if date_frag else ""

    sql = f"SELECT * FROM cross_asset {where} ORDER BY asset, date"
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])


def load_alt_data(
    ticker: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return alt-data rows for one ticker."""
    date_frag, params = _date_clauses(start, end)
    params["ticker"] = ticker
    where = f"WHERE ticker = %(ticker)s" + (f" AND {date_frag}" if date_frag else "")

    sql = f"SELECT * FROM alt_data {where} ORDER BY date"
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])


def load_option_metrics(
    ticker: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return option metrics for one ticker."""
    date_frag, params = _date_clauses(start, end)
    params["ticker"] = ticker
    where = f"WHERE ticker = %(ticker)s" + (f" AND {date_frag}" if date_frag else "")

    sql = f"SELECT * FROM option_metrics {where} ORDER BY date"
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params, parse_dates=["date"])


def load_ticker_metadata() -> pd.DataFrame:
    """Return the entire `ticker_metadata` table."""
    sql = "SELECT * FROM ticker_metadata ORDER BY ticker"
    with get_connection() as conn:
        return pd.read_sql_query(sql, conn)


def load_backtest_results(model_id: int) -> pd.DataFrame:
    """Return walk-forward backtest rows for one model registry id."""
    sql = """
        SELECT * FROM backtest_results
        WHERE model_id = %(mid)s
        ORDER BY date
    """
    with get_connection() as conn:
        return pd.read_sql_query(
            sql, conn, params={"mid": model_id}, parse_dates=["date"],
        )
