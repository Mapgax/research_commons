"""Markdown report rendering for universe merge plans."""

from __future__ import annotations

from datetime import datetime

from research_commons.universe.merge import MergePlan, Refresh
from research_commons.universe.io import UniverseRow

_CATEGORY_TITLES = {
    "collision": "Ticker collisions",
    "superseded_isin": "Superseded / changed ISINs",
    "suspect_non_equity": "Suspected non-equity lines",
    "invalid_isin": "Invalid ISINs",
    "missing_fields": "Incomplete rows",
    "duplicate_in_file": "Conflicting duplicates in file",
    "unknown_isin": "Unknown ISINs (not in universe)",
    "currency_mismatch": "Currency mismatches (warning)",
}


def _row_line(row: UniverseRow) -> str:
    return (
        f"| {row.ticker} | {row.company_name} | {row.isin} | {row.bloomberg_ticker} "
        f"| {row.yahoo_symbol} | {row.currency} | {row.scope_status} |"
    )


_ROW_HEADER = (
    "| ticker | company | ISIN | Bloomberg | Yahoo | ccy | status |\n"
    "|---|---|---|---|---|---|---|"
)


def _refresh_line(refresh: Refresh) -> str:
    changes = "; ".join(
        f"{field}: {old!r} -> {new!r}" for field, (old, new) in refresh.changes.items()
    )
    return f"- **{refresh.row.ticker}** ({refresh.row.isin}): {changes}"


def render_report(
    plan: MergePlan,
    *,
    source_name: str,
    applied: bool,
    now: datetime,
    new_status: str = "watchlist",
) -> str:
    lines: list[str] = []
    quarantined = plan.quarantined
    lines.append(f"# Universe {plan.mode} update — {source_name}")
    lines.append("")
    lines.append(f"*Generated {now:%Y-%m-%d %H:%M:%S} — mode `{plan.mode}`, "
                 f"{'**APPLIED**' if applied else 'plan only (no changes written)'}*")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Universe size after merge: **{len(plan.result)}**")
    if plan.mode == "universe":
        lines.append(f"- New names added (as `{new_status}`): **{len(plan.adds)}**")
        lines.append(f"- Existing names refreshed: **{len(plan.refreshes)}**")
    else:
        lines.append(f"- Focus promotions (-> active): **{len(plan.promotions)}**")
        lines.append(f"- Demotions (active -> inactive): **{len(plan.demotions)}**")
    lines.append(f"- Unchanged: **{plan.unchanged}**")
    lines.append(f"- Quarantined (need manual resolution): **{len(quarantined)}**")
    lines.append(f"- Suppressed (ignore list / superseded): **{len(plan.suppressed)}**")
    lines.append("")

    if plan.mode == "universe" and plan.adds:
        lines.append("## Cost delta")
        lines.append("")
        if new_status == "watchlist":
            lines.append(
                f"- {len(plan.adds)} new names enter as `watchlist`: **metadata only** — "
                "no MSARN price backfill, no Companies_News collection cost until promoted."
            )
            lines.append(
                "- Promote in bounded batches via MSARN's `watchlist_backfill` workflow "
                "(default limit 5), then activate delivery via a focus update."
            )
        else:
            lines.append(
                f"- {len(plan.adds)} new names enter as `{new_status}` (covered): MSARN "
                "will run a FULL historical backfill and Companies_News will collect "
                "news + LLM classification for each of them."
            )
        lines.append("")

    if plan.adds:
        lines.append("## Added")
        lines.append("")
        lines.append(_ROW_HEADER)
        for row in plan.adds:
            lines.append(_row_line(row))
        lines.append("")
        lines.append(
            "> New names are `watchlist`: metadata only. They are **not collected "
            "and not delivered** until promoted (backfill, then a focus update)."
        )
        lines.append("")

    if plan.promotions:
        lines.append("## Focus promotions (-> active)")
        lines.append("")
        lines.append(_ROW_HEADER)
        for row in plan.promotions:
            lines.append(_row_line(row))
        lines.append("")
        lines.append(
            "> Promotions that were `watchlist` before have **no price/feature "
            "history** — run the MSARN backfill for them before relying on "
            "training or dashboard output."
        )
        lines.append("")

    if plan.demotions:
        lines.append("## Demotions (active -> inactive)")
        lines.append("")
        lines.append(_ROW_HEADER)
        for row in plan.demotions:
            lines.append(_row_line(row))
        lines.append("")

    if plan.refreshes:
        lines.append("## Refreshed fields")
        lines.append("")
        for refresh in plan.refreshes:
            lines.append(_refresh_line(refresh))
        lines.append("")

    if quarantined:
        lines.append("## Quarantined — manual resolution required")
        lines.append("")
        for category in _CATEGORY_TITLES:
            group = [i for i in quarantined if i.category == category]
            if not group:
                continue
            lines.append(f"### {_CATEGORY_TITLES[category]}")
            lines.append("")
            for issue in group:
                label = issue.company_name or issue.bloomberg_ticker or issue.isin
                lines.append(f"- **{label}** ({issue.isin or 'no ISIN'}): {issue.detail}")
                lines.append(f"  - Suggested action: {issue.suggestion}")
            lines.append("")

    warnings = plan.warnings
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for issue in warnings:
            label = issue.company_name or issue.bloomberg_ticker or issue.isin
            lines.append(f"- **{label}** ({issue.isin or 'no ISIN'}): {issue.detail}")
            if issue.suggestion:
                lines.append(f"  - Suggested action: {issue.suggestion}")
        lines.append("")

    if plan.suppressed:
        lines.append("## Suppressed (previously resolved, unchanged)")
        lines.append("")
        for update, reason in plan.suppressed:
            lines.append(
                f"- {update.company_name or update.bloomberg_ticker} "
                f"({update.isin or 'no ISIN'}): {reason}"
            )
        lines.append("")

    lines.append("## Post-run checklist")
    lines.append("")
    if applied:
        lines.append("1. Review the git diff of `data/ticker_universe.csv`.")
        lines.append("2. Commit + push research_commons, then each consumer repo "
                     "(exact commands were printed by the tool).")
        lines.append("3. For newly promoted names: run the MSARN backfill.")
    else:
        lines.append("1. Resolve quarantined rows (see suggestions above).")
        lines.append("2. Re-run with `--apply` when the plan looks right.")
    lines.append("")
    return "\n".join(lines)
