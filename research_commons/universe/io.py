"""File I/O for the universe tool: canonical store, ignore list, update files.

All readers are tolerant to BOMs, comma vs semicolon delimiters, header case,
and column supersets (the older 9-column Bloomberg export and the newer
7-column format both parse). The canonical file is written atomically.
"""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path

# Canonical file schema (order matters; ``;``-delimited).
CANONICAL_COLUMNS: tuple[str, ...] = (
    "company_name",
    "ISIN",
    "bloomberg_ticker",
    "ticker",
    "sector_name",
    "currency",
    "yahoo_symbol",
    "scope_status",
    "added_date",
    "superseded_isins",
    "notes",
)

# Columns without which a canonical row cannot be constructed.
REQUIRED_CANONICAL_FIELDS: tuple[str, ...] = (
    "company_name",
    "ISIN",
    "bloomberg_ticker",
    "ticker",
)

# Consumer tickers.csv schema. Audience-routing flags were retired in favour of
# the single scope_status dial; DictReader-based loaders tolerate the change.
CONSUMER_COLUMNS: tuple[str, ...] = (
    "company_name",
    "ISIN",
    "bloomberg_ticker",
    "ticker",
    "sector_name",
    "currency",
    "scope_status",
    "yahoo_symbol",
)

# Update-file header aliases -> canonical field names (all matched lowercased).
_UPDATE_ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": ("company_name", "assetname", "asset_name", "name"),
    "isin": ("isin",),
    "bloomberg_ticker": ("bbticker", "bloomberg_ticker", "bb_ticker"),
    "currency": ("dencurrency", "den_currency", "currency"),
    "sector_name": ("sector_name", "gics_sector", "sector"),
}


@dataclass
class UniverseRow:
    """One canonical universe entry (mirrors CANONICAL_COLUMNS)."""

    company_name: str
    isin: str
    bloomberg_ticker: str
    ticker: str
    sector_name: str = ""
    currency: str = ""
    yahoo_symbol: str = ""
    scope_status: str = "watchlist"
    added_date: str = ""
    superseded_isins: str = ""  # pipe-separated old ISINs for this line
    notes: str = ""

    def superseded_set(self) -> set[str]:
        return {s.strip().upper() for s in self.superseded_isins.split("|") if s.strip()}


@dataclass
class UpdateRow:
    """One parsed row of an uploaded update file."""

    company_name: str
    isin: str
    bloomberg_ticker: str
    currency: str = ""
    sector_name: str = ""
    source_line: int = 0
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class IgnoreEntry:
    isin: str
    bloomberg_ticker: str
    reason: str


def _detect_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        return dialect.delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        return ";" if first_line.count(";") > first_line.count(",") else ","


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        delimiter = _detect_delimiter(sample)
        fh.seek(0)
        reader = csv.DictReader(fh, delimiter=delimiter)
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row = {
                str(k).strip(): ("" if v is None else str(v).strip())
                for k, v in raw_row.items()
                if k is not None
            }
            if any(v for v in row.values()):
                rows.append(row)
        return rows


def _read_xlsx_dicts(path: Path) -> list[dict[str, str]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Reading .xlsx update files requires openpyxl. Install the optional "
            "extra: python3 -m pip install -e '.[universe]'"
        ) from exc

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_cells = next(rows_iter)
    except StopIteration:
        return []
    header = [str(c).strip() if c is not None else "" for c in header_cells]
    rows: list[dict[str, str]] = []
    for cells in rows_iter:
        row = {
            header[i]: ("" if cells[i] is None else str(cells[i]).strip())
            for i in range(min(len(header), len(cells)))
            if header[i]
        }
        if any(v for v in row.values()):
            rows.append(row)
    wb.close()
    return rows


def read_update_file(path: Path) -> list[UpdateRow]:
    """Parse an uploaded update file (xlsx or csv) into UpdateRows."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        raw_rows = _read_xlsx_dicts(path)
    elif suffix in {".csv", ".tsv", ".txt"}:
        raw_rows = _read_csv_dicts(path)
    else:
        raise ValueError(f"Unsupported update file type: {path.name} (use .xlsx or .csv)")

    parsed: list[UpdateRow] = []
    for i, row in enumerate(raw_rows, start=2):  # 1-based + header row
        lowered = {k.lower(): v for k, v in row.items()}

        def pick(field_name: str, lowered_row: dict[str, str] = lowered) -> str:
            for alias in _UPDATE_ALIASES[field_name]:
                value = lowered_row.get(alias)
                if value:
                    return value
            return ""

        parsed.append(
            UpdateRow(
                company_name=pick("company_name"),
                isin=pick("isin").upper(),
                bloomberg_ticker=pick("bloomberg_ticker"),
                currency=pick("currency").upper(),
                sector_name=pick("sector_name"),
                source_line=i,
                raw=row,
            )
        )
    return parsed


def read_canonical(path: Path) -> list[UniverseRow]:
    """Load the canonical universe file."""
    if not path.exists():
        raise FileNotFoundError(
            f"Canonical universe file not found: {path}\n"
            "Seed it first (scripts/seed_universe_from_legacy.py)."
        )
    rows: list[UniverseRow] = []
    valid_fields = {f.name for f in fields(UniverseRow)}
    raw_rows = _read_csv_dicts(path)
    # The canonical file is hand-editable, so a dropped/renamed column must
    # fail with a message naming the file and column — not a TypeError from
    # the dataclass constructor, and not a silently empty universe.
    if raw_rows:
        present = {str(key).strip().lower() for key in raw_rows[0]}
        missing = [c for c in REQUIRED_CANONICAL_FIELDS if c.lower() not in present]
        if missing:
            raise ValueError(
                f"Canonical universe file {path} is missing required column(s): "
                f"{', '.join(missing)}"
            )
    for raw in raw_rows:
        lowered = {(k.lower() if k != "ISIN" else "isin"): v for k, v in raw.items()}
        # Unknown columns are ignored, which is what lets a file written by an
        # older schema (e.g. one still carrying retired audience flags) load.
        kwargs: dict[str, object] = {
            key: value for key, value in lowered.items() if key in valid_fields
        }
        if not kwargs.get("isin") and not kwargs.get("ticker"):
            continue
        rows.append(UniverseRow(**kwargs))  # type: ignore[arg-type]
    return rows


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _rows_to_csv(rows: list[UniverseRow], columns: tuple[str, ...]) -> str:
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        record = []
        for col in columns:
            attr = "isin" if col == "ISIN" else col
            record.append(str(getattr(row, attr)))
        writer.writerow(record)
    return buf.getvalue()


def sort_canonical(rows: list[UniverseRow]) -> list[UniverseRow]:
    """Stable order: ISIN ascending, ISIN-less placeholder rows last.

    This matches the historical tickers.csv ordering, which consumers use as
    the within-status runtime order — keeping it stable keeps their behavior
    stable.
    """
    return sorted(rows, key=lambda r: (r.isin == "", r.isin, r.ticker))


def write_canonical(path: Path, rows: list[UniverseRow]) -> None:
    _atomic_write(path, _rows_to_csv(sort_canonical(rows), CANONICAL_COLUMNS))


def render_consumer_csv(rows: list[UniverseRow]) -> str:
    return _rows_to_csv(sort_canonical(rows), CONSUMER_COLUMNS)


def discard_staged(tmp_path: Path) -> None:
    """Remove a staged temp file, tolerating one that is already gone."""
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def stage_consumer_csv(path: Path, rows: list[UniverseRow]) -> Path:
    """Write the consumer CSV to a temp file beside ``path``; return the temp path.

    Staging separates the fallible part of a write (mkstemp, disk full,
    permissions) from the near-infallible part (rename), so several consumer
    repos can be updated as one all-or-nothing batch — see
    :func:`research_commons.universe.dist.distribute`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(render_consumer_csv(rows))
    except BaseException:
        discard_staged(Path(tmp_name))
        raise
    return Path(tmp_name)


def commit_staged(tmp_path: Path, final_path: Path) -> None:
    """Atomically move a staged file into place."""
    os.replace(tmp_path, final_path)


def read_ignore_list(path: Path) -> list[IgnoreEntry]:
    if not path.exists():
        return []
    entries: list[IgnoreEntry] = []
    for raw in _read_csv_dicts(path):
        lowered = {k.lower(): v for k, v in raw.items()}
        entries.append(
            IgnoreEntry(
                isin=lowered.get("isin", "").upper(),
                bloomberg_ticker=lowered.get("bloomberg_ticker", ""),
                reason=lowered.get("reason", ""),
            )
        )
    return entries


def append_ignore_entries(path: Path, entries: list[IgnoreEntry]) -> None:
    existing = read_ignore_list(path)
    seen = {(e.isin, e.bloomberg_ticker.upper()) for e in existing}
    merged = list(existing)
    for entry in entries:
        if (entry.isin, entry.bloomberg_ticker.upper()) not in seen:
            merged.append(entry)
            seen.add((entry.isin, entry.bloomberg_ticker.upper()))
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(("isin", "bloomberg_ticker", "reason"))
    for entry in merged:
        writer.writerow((entry.isin, entry.bloomberg_ticker, entry.reason))
    _atomic_write(path, buf.getvalue())
