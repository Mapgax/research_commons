#!/usr/bin/env python3
"""One-off: seed data/ticker_universe.csv from the legacy sources.

Reconciles the two pre-centralization sources of truth:

  * Companies_News/tickers.csv (141 rows) — the runtime truth for what the
    consumer pipelines actually collect/deliver today. For ISINs present
    here, these fields win (status, currency, sector, name).
  * live market.ticker_metadata (459 rows) — a superset holding ~318 extra
    names (mostly DB-managed watchlist) that never
    reached the consumer CSVs. For DB-only rows, DB fields win.

Reconciliation rules (documented in the seed report):
  * DB-only rows with scope_status='active' are demoted to 'watchlist':
    they were never in the consumer CSV, so no pipeline ever collected or
    delivered them — 'active' in the DB was aspirational, not operational.
  * Placeholder rows (no ISIN, company_name == ticker) stay as watchlist
    metadata (they may hold FK'd price history and cannot be deleted).
  * Duplicate-ISIN DB rows: the row whose ticker equals its yahoo_symbol
    (the DB-managed convention) is kept; the other is recorded in `notes`.
  * yahoo_symbol: consumer-CSV names get the freshly consolidated derivation
    (research_commons.tickers); DB-only names keep their stored symbol.

Usage:
    python3 scripts/seed_universe_from_legacy.py [--csv PATH] [--out PATH]

Read-only against the DB; writes only the canonical file + seed report.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from research_commons.tickers import derive_yahoo_symbol  # noqa: E402
from research_commons.universe import canonical_path, reports_dir  # noqa: E402
from research_commons.universe.io import UniverseRow, write_canonical  # noqa: E402


def load_consumer_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [
            {k.strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(fh, delimiter=";")
        ]


def load_db_rows() -> list[dict[str, object]]:
    from research_commons.db_market.reads import load_ticker_metadata

    return load_ticker_metadata().to_dict(orient="records")  # type: ignore[no-any-return]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        default="/Users/andreas/Desktop/RBZ_Projects/Companies_News/tickers.csv",
        help="legacy consumer tickers.csv (runtime truth)",
    )
    parser.add_argument("--out", default=str(canonical_path()))
    args = parser.parse_args()

    csv_rows = load_consumer_csv(Path(args.csv))
    db_records = load_db_rows()
    today = datetime.now().date().isoformat()

    notes_log: list[str] = []
    rows: list[UniverseRow] = []
    seen_isins: set[str] = set()
    csv_by_isin = {r["ISIN"]: r for r in csv_rows if r.get("ISIN")}

    # Pass 1: consumer CSV rows (runtime truth wins).
    for record in csv_rows:
        ticker = record.get("ticker", "").upper()
        if not ticker:
            continue
        isin = record.get("ISIN", "").upper()
        bbg = record.get("bloomberg_ticker", "")
        rows.append(
            UniverseRow(
                company_name=record.get("company_name", ""),
                isin=isin,
                bloomberg_ticker=bbg,
                ticker=ticker,
                sector_name=record.get("sector_name", ""),
                currency=record.get("currency", "").upper(),
                yahoo_symbol=derive_yahoo_symbol(ticker, bbg),
                scope_status=record.get("scope_status", "inactive").lower() or "inactive",
                added_date=today,
                notes="",
            )
        )
        if isin:
            seen_isins.add(isin)

    claimed_tickers = {r.ticker for r in rows}

    # Pass 2: DB-only rows. Handle duplicate ISINs first.
    db_by_isin: dict[str, list[dict[str, object]]] = defaultdict(list)
    no_isin_records: list[dict[str, object]] = []
    for record in db_records:
        isin = str(record.get("isin") or "").strip().upper()
        if isin:
            db_by_isin[isin].append(record)
        else:
            no_isin_records.append(record)

    for isin, records in sorted(db_by_isin.items()):
        if isin in seen_isins:
            continue  # consumer CSV already owns this ISIN
        if isin in csv_by_isin:
            continue
        chosen = records[0]
        if len(records) > 1:
            # Prefer the DB-managed convention: ticker == yahoo_symbol.
            preferred = [
                r for r in records
                if str(r.get("ticker")) == str(r.get("yahoo_symbol"))
            ]
            chosen = (preferred or records)[0]
            dropped = [str(r.get("ticker")) for r in records if r is not chosen]
            notes_log.append(
                f"duplicate ISIN {isin}: kept {chosen.get('ticker')}, "
                f"dropped duplicate row(s) {', '.join(dropped)} (still in DB)"
            )

        ticker = str(chosen.get("ticker") or "").strip().upper()
        if not ticker or ticker in claimed_tickers:
            notes_log.append(f"skipped DB row with conflicting/empty ticker for ISIN {isin}")
            continue
        status = str(chosen.get("scope_status") or "watchlist").strip().lower()
        note = ""
        if status == "active":
            # Never operational: not in the consumer CSV -> no pipeline ran it.
            note = "seed: demoted from DB-active (was never in consumer CSV)"
            notes_log.append(f"{ticker} ({isin}): {note}")
            status = "watchlist"
        rows.append(
            UniverseRow(
                company_name=str(chosen.get("company_name") or ""),
                isin=isin,
                bloomberg_ticker=str(chosen.get("bloomberg_ticker") or ""),
                ticker=ticker,
                sector_name=str(chosen.get("sector_name") or ""),
                currency=str(chosen.get("currency") or "USD").strip().upper(),
                yahoo_symbol=str(chosen.get("yahoo_symbol") or "") or ticker,
                scope_status=status,
                added_date=today,
                notes=note,
            )
        )
        claimed_tickers.add(ticker)

    # Pass 3: placeholder rows without ISIN (TSLA/XOM) — keep as watchlist.
    for record in no_isin_records:
        ticker = str(record.get("ticker") or "").strip().upper()
        if not ticker or ticker in claimed_tickers:
            continue
        note = "seed: placeholder row (no ISIN) kept for FK'd history"
        notes_log.append(f"{ticker}: {note}")
        rows.append(
            UniverseRow(
                company_name=str(record.get("company_name") or ticker),
                isin="",
                bloomberg_ticker=str(record.get("bloomberg_ticker") or ""),
                ticker=ticker,
                sector_name=str(record.get("sector_name") or ""),
                currency=str(record.get("currency") or "USD").strip().upper(),
                yahoo_symbol=str(record.get("yahoo_symbol") or "") or ticker,
                scope_status="watchlist",
                added_date=today,
                notes=note,
            )
        )
        claimed_tickers.add(ticker)

    out_path = Path(args.out)
    write_canonical(out_path, rows)

    by_status: dict[str, int] = defaultdict(int)
    for row in rows:
        by_status[row.scope_status] += 1

    reports_dir().mkdir(parents=True, exist_ok=True)
    report_path = reports_dir() / f"{datetime.now():%Y%m%d_%H%M%S}_seed.md"
    lines = [
        "# Canonical universe seed",
        "",
        f"*Generated {datetime.now():%Y-%m-%d %H:%M:%S} by scripts/seed_universe_from_legacy.py*",
        "",
        f"- Consumer CSV rows (runtime truth): {len(csv_rows)}",
        f"- Live DB rows: {len(db_records)}",
        f"- Canonical rows written: {len(rows)} "
        f"({', '.join(f'{k}: {v}' for k, v in sorted(by_status.items()))})",
        "",
        "## Reconciliation notes",
        "",
    ]
    lines += [f"- {note}" for note in notes_log] or ["- none"]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"✓ wrote {out_path} ({len(rows)} rows)")
    print(f"✓ seed report: {report_path}")
    for note in notes_log:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
