"""Typed write helpers for DB1 — `market`.

All writes are idempotent: ON CONFLICT DO UPDATE on the natural key. This makes
it safe to re-run incremental jobs without duplication.

Frozen API surface:

    upsert_prices(rows: list[PriceRow]) -> int
    upsert_fundamentals(rows: list[FundamentalRow]) -> int
    upsert_features(df: pd.DataFrame) -> int
    upsert_macro(rows: list[dict]) -> int
    upsert_cross_asset(rows: list[dict]) -> int
    upsert_alt_data(rows: list[dict]) -> int
    upsert_insider_transactions(rows: list[dict]) -> int
    upsert_option_metrics(rows: list[dict]) -> int
    register_model(...) -> int
    record_backtest_result(...) -> int
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd
from psycopg2.extras import execute_values

from research_commons.db_market.connection import get_connection
from research_commons.types import FundamentalRow, PriceRow

logger = logging.getLogger(__name__)


def upsert_prices(rows: list[PriceRow]) -> int:
    """Insert or update OHLCV rows. Returns number of rows affected."""
    if not rows:
        return 0

    sql = """
        INSERT INTO prices
            (ticker, date, open, high, low, close, adj_close, volume, currency)
        VALUES %s
        ON CONFLICT (ticker, date) DO UPDATE SET
            open      = EXCLUDED.open,
            high      = EXCLUDED.high,
            low       = EXCLUDED.low,
            close     = EXCLUDED.close,
            adj_close = EXCLUDED.adj_close,
            volume    = EXCLUDED.volume,
            currency  = EXCLUDED.currency
    """
    values = [
        (r["ticker"], r["date"], r.get("open"), r.get("high"),
         r.get("low"), r["close"], r.get("adj_close"),
         r.get("volume"), r.get("currency", "USD"))
        for r in rows
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=1000)
            count = cur.rowcount
            conn.commit()
    logger.debug("upsert_prices: %d rows", count)
    return count


def upsert_fundamentals(rows: list[FundamentalRow]) -> int:
    """Insert or update fundamentals snapshots. Returns rows affected."""
    if not rows:
        return 0

    sql = """
        INSERT INTO fundamentals
            (ticker, as_of, trailing_pe, forward_pe, price_to_book,
             debt_to_equity, return_on_equity, profit_margins, ev_to_ebitda)
        VALUES %s
        ON CONFLICT (ticker, as_of) DO UPDATE SET
            trailing_pe      = EXCLUDED.trailing_pe,
            forward_pe       = EXCLUDED.forward_pe,
            price_to_book    = EXCLUDED.price_to_book,
            debt_to_equity   = EXCLUDED.debt_to_equity,
            return_on_equity = EXCLUDED.return_on_equity,
            profit_margins   = EXCLUDED.profit_margins,
            ev_to_ebitda     = EXCLUDED.ev_to_ebitda
    """
    values = [
        (r["ticker"], r["as_of"], r.get("trailing_pe"), r.get("forward_pe"),
         r.get("price_to_book"), r.get("debt_to_equity"),
         r.get("return_on_equity"), r.get("profit_margins"),
         r.get("ev_to_ebitda"))
        for r in rows
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            count = cur.rowcount
            conn.commit()
    return count


def upsert_features(df: pd.DataFrame) -> int:
    """Bulk-upsert the engineered feature matrix.

    The DataFrame must have ``ticker`` and ``date`` columns plus feature
    columns. Dynamically adapts to whatever columns are present.
    """
    if df.empty:
        return 0

    cols = list(df.columns)
    if "ticker" not in cols or "date" not in cols:
        raise ValueError("DataFrame must have 'ticker' and 'date' columns")

    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    update_cols = [c for c in cols if c not in ("ticker", "date")]
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    sql = f"""
        INSERT INTO features ({col_list})
        VALUES ({placeholders})
        ON CONFLICT (ticker, date) DO UPDATE SET
            {update_set}
    """
    records = [tuple(row) for row in df.itertuples(index=False, name=None)]
    with get_connection() as conn:
        with conn.cursor() as cur:
            from psycopg2.extras import execute_batch
            execute_batch(cur, sql, records, page_size=1000)
            count = cur.rowcount
            conn.commit()
    logger.debug("upsert_features: %d rows", count)
    return count


def upsert_macro(rows: list[dict[str, Any]]) -> int:
    """Insert or update macro data rows. Returns rows affected."""
    if not rows:
        return 0

    sql = """
        INSERT INTO macro_data
            (date, vix, vix_5d_change, us10y_yield, us2y_yield,
             yield_curve_10y2y, hy_spread, dollar_index, put_call_ratio)
        VALUES %s
        ON CONFLICT (date) DO UPDATE SET
            vix               = EXCLUDED.vix,
            vix_5d_change     = EXCLUDED.vix_5d_change,
            us10y_yield       = EXCLUDED.us10y_yield,
            us2y_yield        = EXCLUDED.us2y_yield,
            yield_curve_10y2y = EXCLUDED.yield_curve_10y2y,
            hy_spread         = EXCLUDED.hy_spread,
            dollar_index      = EXCLUDED.dollar_index,
            put_call_ratio    = EXCLUDED.put_call_ratio
    """
    values = [
        (r["date"], r.get("vix"), r.get("vix_5d_change"),
         r.get("us10y_yield"), r.get("us2y_yield"),
         r.get("yield_curve_10y2y"), r.get("hy_spread"),
         r.get("dollar_index"), r.get("put_call_ratio"))
        for r in rows
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            count = cur.rowcount
            conn.commit()
    return count


def upsert_cross_asset(rows: list[dict[str, Any]]) -> int:
    """Insert or update cross-asset return rows."""
    if not rows:
        return 0

    sql = """
        INSERT INTO cross_asset (asset, date, close, return_1d)
        VALUES %s
        ON CONFLICT (asset, date) DO UPDATE SET
            close     = EXCLUDED.close,
            return_1d = EXCLUDED.return_1d
    """
    values = [(r["asset"], r["date"], r["close"], r.get("return_1d")) for r in rows]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=1000)
            count = cur.rowcount
            conn.commit()
    return count


def upsert_alt_data(rows: list[dict[str, Any]]) -> int:
    """Insert or update alt-data rows."""
    if not rows:
        return 0

    sql = """
        INSERT INTO alt_data (ticker, date, source, metric, value)
        VALUES %s
        ON CONFLICT (ticker, date, source, metric) DO UPDATE SET
            value = EXCLUDED.value
    """
    values = [
        (r["ticker"], r["date"], r["source"], r["metric"], r.get("value"))
        for r in rows
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=1000)
            count = cur.rowcount
            conn.commit()
    return count


def upsert_insider_transactions(rows: list[dict[str, Any]]) -> int:
    """Insert or update insider transaction rows."""
    if not rows:
        return 0

    sql = """
        INSERT INTO insider_transactions
            (ticker, filing_url, insider_name, transaction_date,
             transaction_type, shares, price)
        VALUES %s
        ON CONFLICT (ticker, filing_url) DO UPDATE SET
            insider_name     = EXCLUDED.insider_name,
            transaction_date = EXCLUDED.transaction_date,
            transaction_type = EXCLUDED.transaction_type,
            shares           = EXCLUDED.shares,
            price            = EXCLUDED.price
    """
    values = [
        (r["ticker"], r["filing_url"], r.get("insider_name"),
         r.get("transaction_date"), r.get("transaction_type"),
         r.get("shares"), r.get("price"))
        for r in rows
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            count = cur.rowcount
            conn.commit()
    return count


def upsert_option_metrics(rows: list[dict[str, Any]]) -> int:
    """Insert or update option metrics rows."""
    if not rows:
        return 0

    sql = """
        INSERT INTO option_metrics
            (ticker, date, atm_iv_30d, atm_iv_60d, put_skew_30d,
             iv_rank_52w, put_call_oi_ratio, call_volume, put_volume)
        VALUES %s
        ON CONFLICT (ticker, date) DO UPDATE SET
            atm_iv_30d       = EXCLUDED.atm_iv_30d,
            atm_iv_60d       = EXCLUDED.atm_iv_60d,
            put_skew_30d     = EXCLUDED.put_skew_30d,
            iv_rank_52w      = EXCLUDED.iv_rank_52w,
            put_call_oi_ratio = EXCLUDED.put_call_oi_ratio,
            call_volume      = EXCLUDED.call_volume,
            put_volume       = EXCLUDED.put_volume
    """
    values = [
        (r["ticker"], r["date"], r.get("atm_iv_30d"), r.get("atm_iv_60d"),
         r.get("put_skew_30d"), r.get("iv_rank_52w"),
         r.get("put_call_oi_ratio"), r.get("call_volume"), r.get("put_volume"))
        for r in rows
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            count = cur.rowcount
            conn.commit()
    return count


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
    sql = """
        INSERT INTO model_registry
            (ticker, target, architecture, checkpoint_path, metrics, config)
        VALUES
            (%(ticker)s, %(target)s, %(architecture)s, %(checkpoint_path)s,
             %(metrics)s::jsonb, %(config)s::jsonb)
        RETURNING id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "ticker": ticker,
                "target": target,
                "architecture": architecture,
                "checkpoint_path": checkpoint_path,
                "metrics": json.dumps(metrics),
                "config": json.dumps(config),
            })
            row = cur.fetchone()
            conn.commit()
    return row[0]


def record_backtest_result(
    *,
    model_id: int,
    rows: list[dict[str, Any]],
) -> int:
    """Append walk-forward backtest rows for a given model id."""
    if not rows:
        return 0

    sql = """
        INSERT INTO backtest_results
            (model_id, date, p_event, realized, decision, metadata)
        VALUES %s
        ON CONFLICT (model_id, date) DO UPDATE SET
            p_event  = EXCLUDED.p_event,
            realized = EXCLUDED.realized,
            decision = EXCLUDED.decision,
            metadata = EXCLUDED.metadata
    """
    values = [
        (model_id, r["date"], r.get("p_event"), r.get("realized"),
         r.get("decision"), json.dumps(r.get("metadata", {})))
        for r in rows
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            count = cur.rowcount
            conn.commit()
    return count
