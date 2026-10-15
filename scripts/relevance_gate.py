#!/usr/bin/env python3
"""Operate the relevance gate in production (docs/relevance-gate-runbook.md).

    # read-only: per-week judged share, failures, p_relevant spread, pass share
    python3 scripts/relevance_gate.py drift
    python3 scripts/relevance_gate.py drift --weeks 12 --threshold 0.6

    # after bumping HAIKU_JUDGE_VERSION: plan (read-only), then re-judge
    python3 scripts/relevance_gate.py rejudge --since 2026-10-01
    python3 scripts/relevance_gate.py rejudge --since 2026-10-01 --apply

``drift`` only reads news_db. ``rejudge --apply`` sends each stale link's
title, 1,000-char body excerpt, ticker and company name to the Haiku judge
(Anthropic, Gemini on fallback) and overwrites ``relevance_judged``; it shares
``RELEVANCE_DAILY_CAP`` with the collect jobs. The Jev judge is not offered
here — it needs the spec's Phase 0 privacy sign-off.

The flag logic below is pure and unit-tested (tests/test_relevance_gate_cli.py).
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from research_commons.db_news.relevance import WeeklyDrift

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = REPO_ROOT / "data" / "ticker_universe.csv"

# Drift flags. Deliberately loose: they say "look at this week", not "act".
MIN_JUDGED_SHARE = 0.90        # complete weeks only; the cap or an outage shows here
PASS_SHARE_SHIFT = 0.10        # article pass share vs the median of earlier weeks
MEDIAN_P_SHIFT = 0.15          # median p_relevant vs the median of earlier weeks
MIN_BASELINE_WEEKS = 2


def _company_names() -> dict[str, str]:
    with UNIVERSE_PATH.open(newline="", encoding="utf-8") as fh:
        return {
            row["ticker"]: row["company_name"]
            for row in csv.DictReader(fh, delimiter=";")
            if row.get("ticker") and row.get("company_name")
        }


# ---------- drift ------------------------------------------------------------


def drift_flags(
    weeks: Sequence[WeeklyDrift], *, today: date, failure_warn_rate: float,
) -> dict[date, list[str]]:
    """Per week, the reasons it deserves a look. The current week is partial,
    so it is never judged on coverage and never used as a baseline; weeks
    before the judge's first attempt are not judged on coverage either."""
    flags: dict[date, list[str]] = {}
    complete = [w for w in weeks if (today - w.week).days >= 7]
    # Weeks before the judge's first attempt were never meant to be judged
    # (the collect jobs only look back a few days), so coverage counts from then.
    live_since = next((w.week for w in weeks if w.n_attempted), None)
    for i, w in enumerate(weeks):
        reasons: list[str] = []
        covered = w in complete and live_since is not None and w.week >= live_since
        if w.failure_rate is not None and w.failure_rate > failure_warn_rate:
            reasons.append(f"failures {w.failure_rate:.0%} > {failure_warn_rate:.0%}")
        if covered and w.judged_share is not None and w.judged_share < MIN_JUDGED_SHARE:
            reasons.append(f"judged {w.judged_share:.0%} < {MIN_JUDGED_SHARE:.0%}")

        baseline = [b for b in complete if b.week < w.week]
        if len(baseline) >= MIN_BASELINE_WEEKS:
            shares = [b.article_pass_share for b in baseline if b.article_pass_share is not None]
            if shares and w.article_pass_share is not None:
                ref = statistics.median(shares)
                if abs(w.article_pass_share - ref) > PASS_SHARE_SHIFT:
                    reasons.append(f"pass share {w.article_pass_share:.0%} vs {ref:.0%}")
            medians = [b.p_median for b in baseline if b.p_median is not None]
            if medians and w.p_median is not None:
                ref = statistics.median(medians)
                if abs(w.p_median - ref) > MEDIAN_P_SHIFT:
                    reasons.append(f"median p {w.p_median:.2f} vs {ref:.2f}")

        if i > 0:
            new = set(w.judges) - set(weeks[i - 1].judges)
            if new and weeks[i - 1].judges:
                reasons.append(f"new judge {', '.join(sorted(new))}")
        if reasons:
            flags[w.week] = reasons
    return flags


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def render_drift(
    weeks: Sequence[WeeklyDrift], flags: dict[date, list[str]], *, threshold: float,
    today: date,
) -> str:
    lines = [
        f"Relevance gate drift — threshold {threshold:.2f}, weeks by articles.published_at",
        "",
        ("week        links  judged  fail   p10  p25  p50  p75  p90  ~0.5  judged≥t  "
         "articles≥t  judges"),
    ]
    for w in weeks:
        q = w.p_quantiles or ()
        qs = "  ".join(f"{v:.2f}" for v in q) if q else "   —     —     —     —     —"
        partial = " (partial)" if (today - w.week).days < 7 else ""
        lines.append(
            f"{w.week}  {w.n_links:>5}  {_pct(w.judged_share):>6}  {_pct(w.failure_rate):>4}  "
            f"{qs}  {_pct(w.near_half_share):>4}  {_pct(w.judged_pass_share):>8}  "
            f"{_pct(w.article_pass_share):>10}  {','.join(w.judges) or '—'}{partial}"
        )
    lines.append("")
    if flags:
        lines.append("Look at:")
        lines += [f"  {week}: {'; '.join(reasons)}" for week, reasons in sorted(flags.items())]
    else:
        lines.append("No flags.")
    return "\n".join(lines)


def cmd_drift(args: argparse.Namespace) -> int:
    from research_commons.config import get_settings
    from research_commons.db_news.relevance import FAILURE_WARN_RATE, weekly_drift

    threshold = get_settings().relevance_threshold if args.threshold is None else args.threshold
    tickers = None if args.all_tickers else sorted(_company_names())
    weeks = weekly_drift(weeks=args.weeks, threshold=threshold, tickers=tickers)
    today = datetime.now(timezone.utc).date()
    flags = drift_flags(weeks, today=today, failure_warn_rate=FAILURE_WARN_RATE)
    print(render_drift(weeks, flags, threshold=threshold, today=today))
    return 0


# ---------- rejudge ----------------------------------------------------------


def _since(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)


def cmd_rejudge(args: argparse.Namespace) -> int:
    from research_commons.config import get_settings
    from research_commons.db_news.relevance import (
        HAIKU_JUDGE_VERSION,
        HaikuRelevanceJudge,
        count_stale_links,
        rejudge_stale_links,
    )

    names = _company_names()
    since = _since(args.since)
    stale = count_stale_links(HAIKU_JUDGE_VERSION, tickers=sorted(names), since=since)
    total = sum(stale.values())
    cap = get_settings().relevance_daily_cap
    print(f"current judge version: {HAIKU_JUDGE_VERSION}; links published since "
          f"{since:%Y-%m-%d} judged by another version: {total}")
    for version, n in stale.items():
        print(f"  {version}: {n}")
    if not total:
        return 0
    bound = cap if args.limit is None else min(args.limit, cap)
    print(f"a run re-judges at most {bound} (RELEVANCE_DAILY_CAP={cap}, shared with the "
          "collect jobs, minus attempts already made today)")
    if not args.apply:
        print("plan only — pass --apply to re-judge")
        return 0

    stats = rejudge_stale_links(
        HaikuRelevanceJudge(), company_names=names, since=since, limit=args.limit,
    )
    print(f"re-judged {stats.judged}, failed {stats.failed} of {stats.selected} selected; "
          f"{stats.cap_remaining} cap remaining today{' (aborted)' if stats.aborted else ''}")
    if total > stats.judged:
        print(f"{total - stats.judged} still stale — run again tomorrow")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    dp = sub.add_parser("drift", help="read-only per-week judge health")
    dp.add_argument("--weeks", type=int, default=8, help="full weeks before the current one")
    dp.add_argument("--threshold", type=float, default=None,
                    help="default RELEVANCE_THRESHOLD")
    dp.add_argument("--all-tickers", action="store_true",
                    help="include links for tickers outside data/ticker_universe.csv")
    dp.set_defaults(func=cmd_drift)

    rp = sub.add_parser("rejudge", help="re-judge links judged by an older judge version")
    rp.add_argument("--since", required=True, metavar="YYYY-MM-DD",
                    help="only links whose article was published on or after this date")
    rp.add_argument("--limit", type=int, default=None, help="at most this many this run")
    rp.add_argument("--apply", action="store_true", help="call the judge and write")
    rp.set_defaults(func=cmd_rejudge)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
