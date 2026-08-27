"""The PM/advisory email-audience file — a second, deliberately separate dial
from ``scope_status`` (which governs collection/coverage).

``data/email_focus_tickers.csv`` (semicolon-delimited) carries:

    ISIN;company_name;scope_pm_ch;scope_pm_de;scope_advisory;email_scope

An ISIN absent from this file counts as ``0`` for all four flags, including
``email_scope`` — i.e. excluded from the email by default. ``email_scope`` is
a real, independently-editable column (not derived at read time): today it's
populated equal to ``scope_pm_ch`` by convention, but the file is the source
of truth and the two can diverge if that's ever wanted.

Retired 2026-07-29 as columns of the main universe file (the old
``scope_pm_ch``/``scope_pm_de``/``scope_advisory`` audience flags — see
``data/archive/retired_scope_flags_20260729.csv``), reinstated 2026-07-29 as
this standalone file after the retirement turned out to remove audience
targeting the email delivery pipeline still needed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from research_commons.scope_policy import ACTIVE, normalize_scope, scopes_for
from research_commons.universe.io import (
    UniverseRow,
    _atomic_write,
    _read_csv_dicts,
    _read_xlsx_dicts,
)

EMAIL_FOCUS_COLUMNS: tuple[str, ...] = (
    "ISIN",
    "company_name",
    "scope_pm_ch",
    "scope_pm_de",
    "scope_advisory",
    "email_scope",
)

_FLAG_FIELDS: tuple[str, ...] = ("scope_pm_ch", "scope_pm_de", "scope_advisory", "email_scope")


@dataclass
class EmailFocusRow:
    isin: str
    company_name: str = ""
    scope_pm_ch: str = "0"
    scope_pm_de: str = "0"
    scope_advisory: str = "0"
    email_scope: str = "0"

    def zeroed_advisory(self) -> "EmailFocusRow":
        return replace(self, scope_advisory="0")

    def fully_zeroed(self) -> "EmailFocusRow":
        return replace(self, scope_pm_ch="0", scope_pm_de="0", scope_advisory="0", email_scope="0")


def _row_from_dict(raw: dict[str, str]) -> EmailFocusRow | None:
    lowered = {(k.lower() if k != "ISIN" else "isin"): v for k, v in raw.items()}
    isin = (lowered.get("isin") or "").strip().upper()
    if not isin:
        return None
    return EmailFocusRow(
        isin=isin,
        company_name=(lowered.get("company_name") or "").strip(),
        scope_pm_ch=_norm_flag(lowered.get("scope_pm_ch")),
        scope_pm_de=_norm_flag(lowered.get("scope_pm_de")),
        scope_advisory=_norm_flag(lowered.get("scope_advisory")),
        email_scope=_norm_flag(lowered.get("email_scope")),
    )


def _norm_flag(value: str | None) -> str:
    return "1" if str(value or "").strip() in {"1", "true", "yes", "y"} else "0"


def read_email_focus(path: Path) -> list[EmailFocusRow]:
    """Read the canonical email_focus_tickers.csv. Missing file = empty list
    (first run before anything has ever been uploaded)."""
    if not path.exists():
        return []
    rows = []
    for raw in _read_csv_dicts(path):
        row = _row_from_dict(raw)
        if row is not None:
            rows.append(row)
    return rows


def read_email_focus_upload(path: Path) -> list[EmailFocusRow]:
    """Parse an uploaded file (xlsx or csv) with the same 6 columns."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        raw_rows = _read_xlsx_dicts(path)
    elif suffix in {".csv", ".tsv", ".txt"}:
        raw_rows = _read_csv_dicts(path)
    else:
        raise ValueError(f"Unsupported update file type: {path.name} (use .xlsx or .csv)")
    rows = []
    for raw in raw_rows:
        row = _row_from_dict(raw)
        if row is not None:
            rows.append(row)
    return rows


def _rows_to_csv(rows: list[EmailFocusRow]) -> str:
    import csv
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(EMAIL_FOCUS_COLUMNS)
    for row in sorted(rows, key=lambda r: r.isin):
        writer.writerow([
            row.isin, row.company_name, row.scope_pm_ch,
            row.scope_pm_de, row.scope_advisory, row.email_scope,
        ])
    return buf.getvalue()


def write_email_focus(path: Path, rows: list[EmailFocusRow]) -> None:
    _atomic_write(path, _rows_to_csv(rows))


def latest_update_file(directory: Path) -> Path | None:
    """Most recently modified .xlsx/.csv in *directory*, or None if empty."""
    candidates = [
        p for p in directory.glob("*")
        if p.is_file() and p.suffix.lower() in {".xlsx", ".xlsm", ".csv", ".tsv", ".txt"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@dataclass
class EmailFocusMergePlan:
    added: list[EmailFocusRow]
    updated: list[tuple[EmailFocusRow, EmailFocusRow]]  # (old, new)
    kept: list[EmailFocusRow]           # removed from upload, resolved to keep (advisory still zeroed)
    zeroed: list[EmailFocusRow]         # removed from upload, resolved to reset
    result: list[EmailFocusRow]         # final full row set


def plan_email_focus_merge(
    current: Iterable[EmailFocusRow],
    upload: Iterable[EmailFocusRow],
    *,
    resolve_removed: Callable[[EmailFocusRow], bool],
) -> EmailFocusMergePlan:
    """Merge an uploaded file into the current email-focus set.

    - ISIN in upload, not currently tracked -> added as-is, no prompt.
    - ISIN in both -> overwritten with the upload's values, no prompt.
    - ISIN currently tracked, absent from upload -> resolve_removed(row)
      decides: True keeps the row's current pm/email values (advisory is
      ALWAYS reset to 0 regardless of this answer — missing means false for
      advisory specifically), False resets every flag to 0.

    resolve_removed is injected so this stays pure/testable; the CLI passes
    an interactive prompt.
    """
    current_by_isin = {r.isin: r for r in current}
    upload_by_isin = {r.isin: r for r in upload}

    added = [row for isin, row in upload_by_isin.items() if isin not in current_by_isin]
    updated = [
        (current_by_isin[isin], row)
        for isin, row in upload_by_isin.items()
        if isin in current_by_isin
    ]

    kept: list[EmailFocusRow] = []
    zeroed: list[EmailFocusRow] = []
    removed_isins = [isin for isin in current_by_isin if isin not in upload_by_isin]
    for isin in removed_isins:
        row = current_by_isin[isin]
        if resolve_removed(row):
            kept.append(row.zeroed_advisory())
        else:
            zeroed.append(row.fully_zeroed())

    result = [row for _, row in updated] + added + kept + zeroed
    return EmailFocusMergePlan(added=added, updated=updated, kept=kept, zeroed=zeroed, result=result)


# ---------------------------------------------------------------------------
# The one place delivery and coverage are *not* independent
# ---------------------------------------------------------------------------
# The two dials are deliberately separate (see the module docstring), with one
# exception running the other way: a name on the delivery list is a name the
# desk is actively working, so it belongs in the tier that gets the daily
# refresh. Collection-wise an emailed inactive name would be fine — news,
# features and enrichment are identical for both tiers — but its risk numbers
# would be up to a week stale, since only ``scoring_daily`` runs every day.
# Detection is taken from scope_policy rather than spelled "active" here, so it
# follows the policy table if the daily cadence ever widens. The rule is
# deliberately one-way and monotone: clearing email_scope never demotes a
# name, because coverage has its own reasons to keep one active.
DELIVERY_SCOPES = scopes_for("scoring_daily")


def delivery_isins(rows: Iterable[EmailFocusRow]) -> set[str]:
    """ISINs the briefing email is actually sent for (``email_scope=1``)."""
    return {row.isin.strip().upper() for row in rows if row.email_scope == "1"}


def delivery_scope_violations(
    universe: Iterable[UniverseRow],
    focus: Iterable[EmailFocusRow],
) -> list[UniverseRow]:
    """Universe rows that are emailed from a tier without a daily refresh."""
    emailed = delivery_isins(focus)
    return [
        row
        for row in universe
        if row.isin.strip().upper() in emailed
        and normalize_scope(row.scope_status) not in DELIVERY_SCOPES
    ]


def promote_delivery_scopes(
    universe: list[UniverseRow],
    focus: Iterable[EmailFocusRow],
) -> tuple[list[UniverseRow], list[UniverseRow]]:
    """Return ``(rows with every violation set to active, the rows changed)``.

    Detection follows the policy table (``DELIVERY_SCOPES``); the repair targets
    ``ACTIVE`` outright, because a set of acceptable tiers does not say which
    one to write. If the daily cadence ever covers a second tier, revisit this
    choice rather than assuming it still follows.

    Always returns a new list, and rebuilds the offending rows rather than
    mutating them, so a caller that plans before it writes still holds the
    untouched originals to diff against.
    """
    offenders = {row.isin.strip().upper() for row in delivery_scope_violations(universe, focus)}
    promoted: list[UniverseRow] = []
    result: list[UniverseRow] = []
    for row in universe:
        if row.isin.strip().upper() in offenders:
            row = replace(row, scope_status=ACTIVE)
            promoted.append(row)
        result.append(row)
    return result, promoted
