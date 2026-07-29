"""DB mirror: schema preflight, canonical -> ticker_metadata sync, drift check.

The central universe tool is the ONLY writer to ``market.ticker_metadata``.
Consumers read their generated ``tickers.csv`` at runtime (CSV-authoritative
during the transition phase); the DB mirror serves tooling, drift checks, and
Idee_Scraping validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from research_commons.universe.io import UniverseRow

# column -> information_schema data_type, as guaranteed by migration 0003.
_EXPECTED_COLUMNS: dict[str, str] = {
    "ticker": "text",
    "company_name": "text",
    "isin": "text",
    "bloomberg_ticker": "text",
    "yahoo_symbol": "text",
    "sector_name": "text",
    "currency": "character",
    "scope_status": "text",
    "added_at": "timestamp with time zone",
    "updated_at": "timestamp with time zone",
    "metadata": "jsonb",
}
# Columns that must be gone: leftovers from superseded schemas. Their presence
# means a migration was not applied and something else may still write them.
_FORBIDDEN_COLUMNS = (
    "active",
    "sector",
    "scope_pm_ch",
    "scope_pm_de",
    "scope_advisory",
)


def preflight_schema() -> list[str]:
    """Compare the live ticker_metadata schema against migration 0003.

    Returns a list of problems (empty = ready). Any problem aborts writes:
    the remedy is ``python3 -c "import research_commons; research_commons.init_market_db()"``.
    """
    from research_commons.db_market.connection import get_connection

    problems: list[str] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'ticker_metadata'
                """
            )
            live = {name: dtype for name, dtype in cur.fetchall()}

    if not live:
        return ["table ticker_metadata does not exist"]
    for column, expected_type in _EXPECTED_COLUMNS.items():
        actual = live.get(column)
        if actual is None:
            problems.append(f"missing column: {column}")
        elif actual != expected_type:
            problems.append(f"column {column}: expected {expected_type}, live has {actual}")
    for column in _FORBIDDEN_COLUMNS:
        if column in live:
            problems.append(
                f"legacy column still present: {column} (migration 0003 not applied)"
            )
    return problems


def sync_canonical_to_db(rows: list[UniverseRow]) -> int:
    """Upsert every canonical row into ticker_metadata. Returns rows affected."""
    from research_commons.db_market.writes import upsert_ticker_metadata

    payload = [
        {
            "ticker": row.ticker,
            "company_name": row.company_name,
            "isin": row.isin or None,
            "bloomberg_ticker": row.bloomberg_ticker or None,
            "yahoo_symbol": row.yahoo_symbol or row.ticker,
            "sector_name": row.sector_name or None,
            "currency": (row.currency or "USD")[:3],
            "scope_status": row.scope_status or "watchlist",
        }
        for row in rows
    ]
    return upsert_ticker_metadata(payload)


@dataclass
class DriftReport:
    missing_in_db: list[str] = field(default_factory=list)
    extra_in_db: list[str] = field(default_factory=list)
    mismatches: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.missing_in_db or self.extra_in_db or self.mismatches)


_DRIFT_FIELDS = (
    "company_name",
    "isin",
    "bloomberg_ticker",
    "yahoo_symbol",
    "sector_name",
    "currency",
    "scope_status",
)


def drift_check(rows: list[UniverseRow]) -> DriftReport:
    """Read-only canonical-vs-DB comparison (fail-loud consumer pre-flight)."""
    from research_commons.db_market.reads import load_ticker_metadata

    df = load_ticker_metadata()
    db_rows = {
        str(record["ticker"]).strip().upper(): record
        for record in df.to_dict(orient="records")
    }
    canonical = {row.ticker.upper(): row for row in rows}

    report = DriftReport()
    report.missing_in_db = sorted(set(canonical) - set(db_rows))
    report.extra_in_db = sorted(set(db_rows) - set(canonical))

    for ticker in sorted(set(canonical) & set(db_rows)):
        row = canonical[ticker]
        record = db_rows[ticker]
        diffs: dict[str, tuple[str, str]] = {}
        for field_name in _DRIFT_FIELDS:
            canonical_norm = str(getattr(row, field_name) or "").strip()
            db_norm = str(record.get(field_name) or "").strip()
            if canonical_norm != db_norm:
                diffs[field_name] = (canonical_norm, db_norm)
        if diffs:
            report.mismatches[ticker] = diffs
    return report
