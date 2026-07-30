"""Ticker universe coverage & freshness dashboard.

Read-only Streamlit report over market_db: what's in the universe (scope_status,
sector, PM/advisory email flags), what data is actually loaded per ticker
(prices/fundamentals/features/alt_data/insider_transactions/option_metrics),
and how fresh/complete it is. Built to answer "which tickers are missing a
proper backfill?" at a glance instead of one-off SQL each time.

Run from the repo root (matches this repo's env-loading convention):

    set -a && source .env && set +a
    streamlit run reports/ticker_universe_report.py

This is a reporting tool only — it never writes. It opts into the
`sector_reader` read-only DB role (see CLAUDE.md) for defense in depth,
without touching the shared .env `MARKET_ROLE` setting that every writer
process also reads.
"""

from __future__ import annotations

import os

os.environ.setdefault("MARKET_ROLE", "sector_reader")

from datetime import date, datetime

import pandas as pd
import streamlit as st

from research_commons.db_market.connection import get_connection
from research_commons.universe import email_focus_path
from research_commons.universe.email_focus import read_email_focus

st.set_page_config(page_title="Ticker Universe Coverage", layout="wide")

# Per-ticker time series tables and their date column. macro_data / cross_asset
# are market-wide, not per-ticker, so they're intentionally excluded here.
DATA_TABLES: dict[str, str] = {
    "prices": "date",
    "fundamentals": "as_of",
    "features": "date",
    "alt_data": "date",
    "insider_transactions": "transaction_date",
    "option_metrics": "date",
}

MIN_PRICE_ROWS_FOR_FEATURES = 60  # mirrors MSARN scheduler/daily_update.py
STALE_DAYS_DEFAULT = 5


@st.cache_data(ttl=600, show_spinner="Loading ticker metadata…")
def load_ticker_metadata() -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql(
            """
            SELECT ticker, company_name, isin, sector_name, currency,
                   scope_status, bloomberg_ticker, yahoo_symbol,
                   added_at, updated_at
            FROM ticker_metadata
            ORDER BY ticker
            """,
            conn,
        )


@st.cache_data(ttl=600, show_spinner="Loading coverage per data table…")
def load_coverage(table: str, date_col: str) -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql(
            f"""
            SELECT ticker,
                   COUNT(*) AS rows,
                   MIN({date_col}) AS start_date,
                   MAX({date_col}) AS end_date
            FROM {table}
            GROUP BY ticker
            """,
            conn,
        )


@st.cache_data(ttl=600, show_spinner="Loading email-focus audience file…")
def load_email_focus() -> pd.DataFrame:
    rows = read_email_focus(email_focus_path())
    if not rows:
        return pd.DataFrame(
            columns=["isin", "scope_pm_ch", "scope_pm_de", "scope_advisory", "email_scope"]
        )
    df = pd.DataFrame([r.__dict__ for r in rows])
    for col in ("scope_pm_ch", "scope_pm_de", "scope_advisory", "email_scope"):
        df[col] = df[col].astype(int)
    return df


def build_universe_table() -> pd.DataFrame:
    meta = load_ticker_metadata()
    focus = load_email_focus()
    merged = meta.merge(focus.drop(columns=["company_name"], errors="ignore"), on="isin", how="left")
    for col in ("scope_pm_ch", "scope_pm_de", "scope_advisory", "email_scope"):
        merged[col] = merged[col].fillna(0).astype(int)
    return merged


def build_coverage_matrix() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Wide per-ticker matrix (rows/staleness per table) + the raw per-table frames."""
    per_table: dict[str, pd.DataFrame] = {}
    wide = None
    today = pd.Timestamp(date.today())

    for table, date_col in DATA_TABLES.items():
        cov = load_coverage(table, date_col)
        cov["start_date"] = pd.to_datetime(cov["start_date"])
        cov["end_date"] = pd.to_datetime(cov["end_date"])
        cov["days_stale"] = (today - cov["end_date"]).dt.days
        per_table[table] = cov

        renamed = cov.rename(
            columns={
                "rows": f"{table}_rows",
                "start_date": f"{table}_start",
                "end_date": f"{table}_end",
                "days_stale": f"{table}_stale_days",
            }
        )
        wide = renamed if wide is None else wide.merge(renamed, on="ticker", how="outer")

    return wide, per_table


def _format_date_col(col: pd.Series) -> pd.Series:
    """NaT -> "" (blank in the table) instead of the literal string "None"."""
    return col.apply(lambda d: d.date().isoformat() if pd.notna(d) else "")


def main() -> None:
    st.title("Ticker Universe — Coverage & Freshness")
    st.caption(
        "Metadata + PM/advisory audience from `data/email_focus_tickers.csv` "
        "(the two are deliberately independent — see CLAUDE.md)."
    )

    if st.sidebar.button("Refresh data (clear cache)"):
        st.cache_data.clear()

    universe = build_universe_table()
    wide, per_table = build_coverage_matrix()
    full = universe.merge(wide, on="ticker", how="left")
    full["sector_name"] = full["sector_name"].fillna("(none)")

    # ── Sidebar filters ──────────────────────────────────────────────────
    st.sidebar.header("Filters")
    scopes = sorted(full["scope_status"].dropna().unique().tolist())
    scope_filter = st.sidebar.multiselect("Scope status", scopes, default=scopes)
    sectors = sorted(full["sector_name"].unique().tolist())
    sector_filter = st.sidebar.multiselect("Sector", sectors, default=sectors)
    stale_threshold = st.sidebar.slider("Flag prices as stale after (days)", 1, 30, STALE_DAYS_DEFAULT)
    search = st.sidebar.text_input("Search ticker / company name")
    issues_only = st.sidebar.checkbox("Show only tickers with a data-quality issue", value=False)

    filtered = full[full["scope_status"].isin(scope_filter) & full["sector_name"].isin(sector_filter)]
    if search:
        s = search.strip().lower()
        filtered = filtered[
            filtered["ticker"].str.lower().str.contains(s)
            | filtered["company_name"].str.lower().str.contains(s)
        ]

    # ── Data-quality flags (drives both the alerts tab and KPI row) ────────
    price_rows = filtered.get("prices_rows", pd.Series(dtype=float)).fillna(0)
    price_stale = filtered.get("prices_stale_days", pd.Series(dtype=float))
    covered = filtered["scope_status"].isin(["active", "inactive"])

    filtered = filtered.assign(
        issue_zero_prices=covered & (price_rows == 0),
        issue_below_min_history=covered & (price_rows > 0) & (price_rows < MIN_PRICE_ROWS_FOR_FEATURES),
        issue_stale_prices=covered & price_stale.gt(stale_threshold).fillna(False),
        issue_missing_features=covered & (price_rows >= MIN_PRICE_ROWS_FOR_FEATURES)
        & (filtered.get("features_rows", pd.Series(dtype=float)).fillna(0) == 0),
    )
    filtered["has_issue"] = filtered[
        ["issue_zero_prices", "issue_below_min_history", "issue_stale_prices", "issue_missing_features"]
    ].any(axis=1)

    view = filtered[filtered["has_issue"]] if issues_only else filtered

    # ── KPI row ──────────────────────────────────────────────────────────
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Tickers (filtered)", len(filtered))
    k2.metric("Active", int((filtered["scope_status"] == "active").sum()))
    k3.metric("Inactive", int((filtered["scope_status"] == "inactive").sum()))
    k4.metric("Watchlist", int((filtered["scope_status"] == "watchlist").sum()))
    k5.metric("With data-quality issue", int(filtered["has_issue"].sum()))

    tab_universe, tab_coverage, tab_alerts = st.tabs(
        ["Universe & metadata", "Coverage & freshness", "Data-quality alerts"]
    )

    with tab_universe:
        st.dataframe(
            view[
                [
                    "ticker", "company_name", "isin", "sector_name", "currency",
                    "scope_status", "scope_pm_ch", "scope_pm_de", "scope_advisory",
                    "email_scope", "bloomberg_ticker", "yahoo_symbol",
                ]
            ],
            width="stretch",
            hide_index=True,
        )

    with tab_coverage:
        st.caption("Row count and start–end date per table. Blank = no rows loaded.")
        cols = ["ticker", "scope_status"]
        for table in DATA_TABLES:
            cols += [f"{table}_rows", f"{table}_start", f"{table}_end"]
        display = view[cols].copy()
        for table in DATA_TABLES:
            display[f"{table}_rows"] = display[f"{table}_rows"].fillna(0).astype(int)
            for c in (f"{table}_start", f"{table}_end"):
                display[c] = _format_date_col(display[c])
        st.dataframe(display, width="stretch", hide_index=True)

    with tab_alerts:
        st.caption(
            f"Covered tickers (active/inactive) only. Stale threshold: {stale_threshold} days. "
            f"Min-history threshold: {MIN_PRICE_ROWS_FOR_FEATURES} price rows (MSARN feature-engineering floor)."
        )
        alert_cols = [
            "ticker", "company_name", "scope_status", "prices_rows", "prices_end",
            "issue_zero_prices", "issue_below_min_history", "issue_stale_prices", "issue_missing_features",
        ]
        alerts = filtered[filtered["has_issue"]][alert_cols].copy()
        alerts["prices_rows"] = alerts["prices_rows"].fillna(0).astype(int)
        alerts["prices_end"] = _format_date_col(alerts["prices_end"])
        st.dataframe(alerts, width="stretch", hide_index=True)
        st.caption(f"Generated {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
