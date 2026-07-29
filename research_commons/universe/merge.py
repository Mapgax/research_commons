"""Pure merge + issue-detection engine for universe/focus updates.

Everything here is side-effect free: given the canonical rows, the parsed
update rows, and the ignore list, produce a :class:`MergePlan` describing
adds, refreshes, promotions, demotions, quarantined issues, and the resulting
canonical state. The CLI decides whether to persist it (``--apply``).

Merge key is **ISIN**. Canonical tickers are derived only for brand-new
ISINs; an existing row never has its ticker changed by a merge (use the
explicit ``rename-ticker`` command, which cascades through the DB FKs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date

from research_commons.tickers import (
    BLOOMBERG_EXCHANGE_TO_CURRENCY,
    derive_yahoo_symbol,
    normalize_ticker,
    parse_bloomberg_to_currency,
)
from research_commons.universe.io import IgnoreEntry, UniverseRow, UpdateRow

# Issue categories, most severe first.
COLLISION = "collision"
SUPERSEDED_ISIN = "superseded_isin"
SUSPECT_NON_EQUITY = "suspect_non_equity"
INVALID_ISIN = "invalid_isin"
MISSING_FIELDS = "missing_fields"
DUPLICATE_IN_FILE = "duplicate_in_file"
UNKNOWN_ISIN = "unknown_isin"
CURRENCY_MISMATCH = "currency_mismatch"
TICKER_STALE = "ticker_stale"

_QUARANTINE_CATEGORIES = {
    COLLISION,
    SUPERSEDED_ISIN,
    SUSPECT_NON_EQUITY,
    INVALID_ISIN,
    MISSING_FIELDS,
    DUPLICATE_IN_FILE,
    UNKNOWN_ISIN,
}

_NON_EQUITY_NAME_RE = re.compile(
    r"[-\s(]\s*(RTS?|RIGHTS?|WTS?|WARRANTS?|SUBSCRIPTION|TENDER)\s*[)\s]*$|"
    r"\b(RIGHTS?|WARRANTS?|SUBSCRIPTION)\b",
    re.IGNORECASE,
)
# Share classes A/B/C are legitimate (BRK/B, SCA/B ...); anything else after a
# slash (e.g. IBE/D rights line) is structurally suspect. A bare trailing
# slash ("UU/ LN") is Bloomberg shorthand, not a share class.
_ALLOWED_SHARE_CLASSES = {"", "A", "B", "C"}


@dataclass
class Issue:
    category: str
    isin: str
    bloomberg_ticker: str
    company_name: str
    detail: str
    suggestion: str

    @property
    def quarantined(self) -> bool:
        return self.category in _QUARANTINE_CATEGORIES


@dataclass
class Refresh:
    row: UniverseRow
    changes: dict[str, tuple[str, str]]  # field -> (old, new)


@dataclass
class MergePlan:
    mode: str
    result: list[UniverseRow] = field(default_factory=list)
    adds: list[UniverseRow] = field(default_factory=list)
    refreshes: list[Refresh] = field(default_factory=list)
    promotions: list[UniverseRow] = field(default_factory=list)
    demotions: list[UniverseRow] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    suppressed: list[tuple[UpdateRow, str]] = field(default_factory=list)  # (row, reason)
    unchanged: int = 0

    @property
    def quarantined(self) -> list[Issue]:
        return [i for i in self.issues if i.quarantined]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if not i.quarantined]


def isin_is_valid(isin: str) -> bool:
    """Validate an ISIN: format + Luhn check digit over letter-expanded digits."""
    isin = (isin or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
        return False
    expanded = "".join(str(int(ch, 36)) for ch in isin)
    digits = [int(d) for d in expanded]
    total = 0
    # Luhn: double every second digit from the right.
    for offset, digit in enumerate(reversed(digits)):
        if offset % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def looks_like_non_equity(row: UpdateRow) -> str:
    """Return a human-readable reason when the row is structurally suspect."""
    name = row.company_name or ""
    if _NON_EQUITY_NAME_RE.search(name):
        return f"company name {name!r} matches rights/warrant pattern"
    base = (row.bloomberg_ticker or "").strip().upper().split()[0] if row.bloomberg_ticker else ""
    if "/" in base:
        share_class = base.split("/", 1)[1]
        if share_class not in _ALLOWED_SHARE_CLASSES:
            return (
                f"Bloomberg base {base!r} carries slash token {share_class!r} "
                "(not a known share class)"
            )
    return ""


def _bloomberg_base(bbg: str) -> str:
    return normalize_ticker(bbg)


def _matches_ignore(row: UpdateRow, ignore: list[IgnoreEntry]) -> IgnoreEntry | None:
    bbg = (row.bloomberg_ticker or "").strip().upper()
    for entry in ignore:
        if entry.isin and entry.isin == row.isin:
            return entry
        if not entry.isin and entry.bloomberg_ticker and entry.bloomberg_ticker.upper() == bbg:
            return entry
    return None


def _dedupe_update_rows(
    updates: list[UpdateRow], plan: MergePlan
) -> list[UpdateRow]:
    """Collapse identical duplicate ISIN rows; flag conflicting ones."""
    by_isin: dict[str, UpdateRow] = {}
    deduped: list[UpdateRow] = []
    for row in updates:
        if not row.isin:
            deduped.append(row)
            continue
        prior = by_isin.get(row.isin)
        if prior is None:
            by_isin[row.isin] = row
            deduped.append(row)
            continue
        if (prior.company_name, prior.bloomberg_ticker) == (
            row.company_name,
            row.bloomberg_ticker,
        ):
            continue  # identical duplicate — collapse silently
        plan.issues.append(
            Issue(
                category=DUPLICATE_IN_FILE,
                isin=row.isin,
                bloomberg_ticker=row.bloomberg_ticker,
                company_name=row.company_name,
                detail=(
                    f"line {row.source_line}: ISIN appears twice with conflicting data "
                    f"(also line {prior.source_line}: {prior.company_name!r} "
                    f"/ {prior.bloomberg_ticker!r})"
                ),
                suggestion="Fix the update file so each ISIN appears once, then re-run.",
            )
        )
    return deduped


def _refresh_existing(
    canonical_row: UniverseRow, update: UpdateRow, plan: MergePlan
) -> UniverseRow:
    """Apply refreshable fields from an update row onto an existing entry."""
    changes: dict[str, tuple[str, str]] = {}
    updated = canonical_row

    if update.company_name and update.company_name != canonical_row.company_name:
        changes["company_name"] = (canonical_row.company_name, update.company_name)
        updated = replace(updated, company_name=update.company_name)
    if update.sector_name and update.sector_name != canonical_row.sector_name:
        changes["sector_name"] = (canonical_row.sector_name, update.sector_name)
        updated = replace(updated, sector_name=update.sector_name)
    if update.bloomberg_ticker and update.bloomberg_ticker != canonical_row.bloomberg_ticker:
        changes["bloomberg_ticker"] = (
            canonical_row.bloomberg_ticker,
            update.bloomberg_ticker,
        )
        updated = replace(updated, bloomberg_ticker=update.bloomberg_ticker)
        # A moved listing can change the derived Yahoo symbol; recompute unless
        # the current symbol came from a manual override or dotted canonical.
        derived = derive_yahoo_symbol(updated.ticker, update.bloomberg_ticker)
        if derived and derived != updated.yahoo_symbol:
            changes["yahoo_symbol"] = (updated.yahoo_symbol, derived)
            updated = replace(updated, yahoo_symbol=derived)
        # A replaced listing (SOLB -> LOTB, WTRG -> EVR) leaves the canonical
        # ticker pointing at a security that no longer exists under that
        # symbol. Never rename automatically — the DB PK cascade is a
        # deliberate act — but the operator must be told it is needed.
        new_base = normalize_ticker(update.bloomberg_ticker)
        if new_base and new_base != canonical_row.ticker and "." not in canonical_row.ticker:
            plan.issues.append(
                Issue(
                    category=TICKER_STALE,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=(
                        f"line {update.source_line}: Bloomberg line moved to "
                        f"{update.bloomberg_ticker!r}; canonical ticker "
                        f"{canonical_row.ticker!r} no longer matches its base {new_base!r}"
                    ),
                    suggestion=(
                        "If the listing was replaced, rename it (this cascades the DB "
                        "history): python3 -m research_commons.universe rename-ticker "
                        f"{canonical_row.ticker} {new_base} --apply"
                    ),
                )
            )
    if update.currency and canonical_row.currency and (
        update.currency != canonical_row.currency
    ):
        # The currency column in uploads (DenCurrency) is the *denomination*
        # currency and is often wrong about the trading currency (e.g. EUR
        # lines of LSE-listed names). Trading currency drives FX handling
        # downstream, so it is never refreshed automatically — only flagged.
        plan.issues.append(
            Issue(
                category=CURRENCY_MISMATCH,
                isin=update.isin,
                bloomberg_ticker=update.bloomberg_ticker,
                company_name=update.company_name,
                detail=(
                    f"line {update.source_line}: file says {update.currency}, canonical "
                    f"keeps {canonical_row.currency} (currency is never auto-refreshed)"
                ),
                suggestion=(
                    "If the canonical value is actually wrong, edit "
                    "data/ticker_universe.csv by hand."
                ),
            )
        )

    if changes:
        plan.refreshes.append(Refresh(row=updated, changes=changes))
    else:
        plan.unchanged += 1
    return updated


def plan_universe_merge(
    canonical: list[UniverseRow],
    updates: list[UpdateRow],
    ignore: list[IgnoreEntry],
    *,
    new_status: str = "watchlist",
    resolutions: dict[str, str] | None = None,
    today: date | None = None,
) -> MergePlan:
    """Additive general update: expand the universe, refresh existing rows.

    Never deletes, never demotes. New ISINs enter with ``new_status``
    (default ``watchlist``: metadata only, no backfill, no collection cost)
    (metadata only: no backfill, no collection cost until promoted).
    """
    plan = MergePlan(mode="universe")
    resolutions = {k.upper(): v.upper() for k, v in (resolutions or {}).items()}
    today = today or date.today()

    by_isin: dict[str, int] = {r.isin: i for i, r in enumerate(canonical) if r.isin}
    superseded_to_index: dict[str, int] = {}
    for i, row in enumerate(canonical):
        for old in row.superseded_set():
            superseded_to_index[old] = i
    by_bbg: dict[str, int] = {
        r.bloomberg_ticker.strip().upper(): i
        for i, r in enumerate(canonical)
        if r.bloomberg_ticker
    }
    result = list(canonical)
    claimed_tickers = {r.ticker for r in canonical}
    file_isins = {u.isin for u in updates if u.isin}

    for update in _dedupe_update_rows(updates, plan):
        # 1. Required fields.
        if not (update.isin and update.company_name and update.bloomberg_ticker):
            missing = [
                name
                for name, value in (
                    ("ISIN", update.isin),
                    ("company name", update.company_name),
                    ("Bloomberg ticker", update.bloomberg_ticker),
                )
                if not value
            ]
            plan.issues.append(
                Issue(
                    category=MISSING_FIELDS,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=f"line {update.source_line}: missing {', '.join(missing)}",
                    suggestion="Complete the row in the update file and re-run.",
                )
            )
            continue

        # 2. Ignore list.
        entry = _matches_ignore(update, ignore)
        if entry is not None:
            plan.suppressed.append((update, entry.reason or "on ignore list"))
            continue

        # 3. ISIN checksum.
        if not isin_is_valid(update.isin):
            plan.issues.append(
                Issue(
                    category=INVALID_ISIN,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=f"line {update.source_line}: ISIN fails format/checksum validation",
                    suggestion="Correct the ISIN in the update file and re-run.",
                )
            )
            continue

        # 4. Known ISIN -> refresh.
        if update.isin in by_isin:
            idx = by_isin[update.isin]
            result[idx] = _refresh_existing(result[idx], update, plan)
            continue

        # 5. Superseded ISIN already recorded on an existing row.
        if update.isin in superseded_to_index:
            row = canonical[superseded_to_index[update.isin]]
            plan.suppressed.append(
                (update, f"superseded ISIN of {row.ticker} (now {row.isin})")
            )
            continue

        # 6. Structurally suspect lines (rights, warrants, odd share classes).
        reason = looks_like_non_equity(update)
        if reason:
            plan.issues.append(
                Issue(
                    category=SUSPECT_NON_EQUITY,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=f"line {update.source_line}: {reason}",
                    suggestion=(
                        "If this is not a tradable common equity line, add it to "
                        "data/universe_ignore.csv; otherwise re-run with "
                        f"--resolve {update.isin}=<TICKER> to force-add."
                    ),
                )
            )
            continue

        # 7. Same Bloomberg line, different ISIN -> corporate action suspect.
        bbg_key = update.bloomberg_ticker.strip().upper()
        if bbg_key in by_bbg:
            existing = result[by_bbg[bbg_key]]
            if existing.isin in file_isins:
                detail = (
                    f"line {update.source_line}: carries a superseded ISIN for existing "
                    f"ticker {existing.ticker} — the file also contains its current "
                    f"ISIN {existing.isin}"
                )
                suggestion = (
                    "Old line from before an ISIN change. Add to "
                    "data/universe_ignore.csv (reason: superseded ISIN)."
                )
            else:
                detail = (
                    f"line {update.source_line}: same Bloomberg line as existing ticker "
                    f"{existing.ticker} but different ISIN ({existing.isin} -> "
                    f"{update.isin}) — possible ISIN change"
                )
                suggestion = (
                    f"If this is an ISIN change, update the {existing.ticker} row: set "
                    f"ISIN={update.isin} and append {existing.isin} to its "
                    "superseded_isins; then re-run."
                )
            plan.issues.append(
                Issue(
                    category=SUPERSEDED_ISIN,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=detail,
                    suggestion=suggestion,
                )
            )
            continue

        # 8. Brand-new name: derive the canonical ticker.
        resolved = resolutions.get(update.isin, "")
        ticker = resolved or _bloomberg_base(update.bloomberg_ticker)
        if not ticker:
            plan.issues.append(
                Issue(
                    category=MISSING_FIELDS,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=f"line {update.source_line}: cannot derive a ticker",
                    suggestion=f"Re-run with --resolve {update.isin}=<TICKER>.",
                )
            )
            continue
        if ticker in claimed_tickers:
            suggested = derive_yahoo_symbol(ticker, update.bloomberg_ticker)
            if resolved:
                detail = (
                    f"line {update.source_line}: resolution {resolved!r} is already "
                    "taken by another company"
                )
            else:
                detail = (
                    f"line {update.source_line}: derived ticker {ticker!r} is already "
                    "taken by a different company"
                )
            plan.issues.append(
                Issue(
                    category=COLLISION,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=detail,
                    suggestion=(
                        f"Re-run with --resolve {update.isin}=<TICKER>; suggested: "
                        f"{suggested} (dot-suffix style, matches its Yahoo symbol)."
                    ),
                )
            )
            continue

        # Trading currency comes from the exchange code when we know it; the
        # upload's DenCurrency is a denomination currency and loses on
        # conflict (it claims EUR for LSE-listed names, USD for HK lines).
        parts = update.bloomberg_ticker.strip().upper().split()
        exchange_code = parts[1] if len(parts) >= 2 else ""
        derived_ccy = parse_bloomberg_to_currency(update.bloomberg_ticker)
        if exchange_code in BLOOMBERG_EXCHANGE_TO_CURRENCY:
            currency = derived_ccy
            if update.currency and update.currency != derived_ccy:
                plan.issues.append(
                    Issue(
                        category=CURRENCY_MISMATCH,
                        isin=update.isin,
                        bloomberg_ticker=update.bloomberg_ticker,
                        company_name=update.company_name,
                        detail=(
                            f"line {update.source_line}: file says {update.currency}, "
                            f"exchange code implies {derived_ccy} — using {derived_ccy}"
                        ),
                        suggestion=(
                            "DenCurrency is a denomination currency; correct "
                            "data/ticker_universe.csv by hand if the exchange-derived "
                            "value is wrong."
                        ),
                    )
                )
        else:
            currency = update.currency or derived_ccy

        new_row = UniverseRow(
            company_name=update.company_name,
            isin=update.isin,
            bloomberg_ticker=update.bloomberg_ticker,
            ticker=ticker,
            sector_name=update.sector_name,
            currency=currency,
            yahoo_symbol=derive_yahoo_symbol(ticker, update.bloomberg_ticker),
            scope_status=new_status,
            added_date=today.isoformat(),
        )
        result.append(new_row)
        plan.adds.append(new_row)
        claimed_tickers.add(ticker)
        by_isin[update.isin] = len(result) - 1
        by_bbg[bbg_key] = len(result) - 1

    plan.result = result
    return plan


def plan_focus_merge(
    canonical: list[UniverseRow],
    updates: list[UpdateRow],
    ignore: list[IgnoreEntry],
) -> MergePlan:
    """Focus snapshot: ISIN in file -> active; previously-active absent -> inactive.

    Focus files never touch universe membership: unknown ISINs are quarantined
    (add them via a general universe update first).
    """
    plan = MergePlan(mode="focus")
    by_isin = {r.isin: i for i, r in enumerate(canonical) if r.isin}
    result = list(canonical)
    focus_isins: set[str] = set()

    for update in _dedupe_update_rows(updates, plan):
        if not update.isin:
            plan.issues.append(
                Issue(
                    category=MISSING_FIELDS,
                    isin="",
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=f"line {update.source_line}: missing ISIN",
                    suggestion="Complete the row in the focus file and re-run.",
                )
            )
            continue
        entry = _matches_ignore(update, ignore)
        if entry is not None:
            plan.suppressed.append((update, entry.reason or "on ignore list"))
            continue
        idx = by_isin.get(update.isin)
        if idx is None:
            plan.issues.append(
                Issue(
                    category=UNKNOWN_ISIN,
                    isin=update.isin,
                    bloomberg_ticker=update.bloomberg_ticker,
                    company_name=update.company_name,
                    detail=f"line {update.source_line}: ISIN is not in the universe",
                    suggestion=(
                        "Add it via a general universe update first "
                        "(ingest --mode universe), then re-run the focus update."
                    ),
                )
            )
            continue
        focus_isins.add(update.isin)
        row = result[idx]
        if row.scope_status != "active":
            promoted = replace(row, scope_status="active")
            result[idx] = promoted
            plan.promotions.append(promoted)
        else:
            plan.unchanged += 1

    for i, row in enumerate(result):
        if row.scope_status == "active" and row.isin and row.isin not in focus_isins:
            demoted = replace(row, scope_status="inactive")
            result[i] = demoted
            plan.demotions.append(demoted)

    plan.result = result
    return plan
