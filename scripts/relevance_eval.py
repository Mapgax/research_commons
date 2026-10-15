#!/usr/bin/env python3
"""Offline eval for the relevance gate (docs/spec-jev-relevance-gate.md, Phase 2).

Two steps:

    # 1. draw a stratified sample of real links into a CSV for hand-labelling
    python3 scripts/relevance_eval.py sample
    python3 scripts/relevance_eval.py sample --hard 123456:AAPL --hard 123789:GE

    # 2. after filling the `label` column (relevant / not_relevant / unsure)
    python3 scripts/relevance_eval.py run --arms prior,haiku
    python3 scripts/relevance_eval.py run --arms prior,haiku,jev \\
        --jev-price-in <usd/Mtok> --jev-price-out <usd/Mtok>

Everything lands under ``eval/relevance/`` which is git-ignored: the labels are
R&Co coverage decisions and stay on this machine.

Privacy ⚠️
``sample`` only reads news_db. ``run`` sends each labelled link's title,
1,000-char body excerpt, ticker and company name to every judge arm:
Anthropic (Gemini on fallback) for ``haiku``, TypeSafe for ``jev``. The Jev arm
needs the Phase 0 sign-off and refuses to start without
``RELEVANCE_JEV_OPT_IN=true``. ``prior`` sends nothing.

The metric functions below are pure and unit-tested
(tests/test_relevance_eval_metrics.py); the CLI around them is not.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "eval" / "relevance"
LABELS_PATH = EVAL_DIR / "labels.csv"
UNIVERSE_PATH = REPO_ROOT / "data" / "ticker_universe.csv"

LABEL_COLUMNS = [
    "article_id", "ticker", "company_name", "stratum", "source", "prior",
    "title", "body_excerpt", "label", "notes",
]
LABEL_VALUES = {"relevant": 1, "not_relevant": 0}

MIN_RECALL = 0.90

# Decision rule — fixed before the eval runs (spec, "Decision rule").
JEV_MAX_AUC_GAP = 0.02
JEV_MAX_COST_RATIO = 0.50
JEV_MAX_FAILURE_RATE = 0.02
MIN_AUC_GAIN_OVER_PRIOR = 0.05


# ---------- metrics (pure) ---------------------------------------------------


def roc_auc(y: Sequence[int], p: Sequence[float]) -> float | None:
    """Mann-Whitney AUC with average ranks for ties. None if a class is absent."""
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    order = sorted(range(len(p)), key=lambda i: p[i])
    ranks = [0.0] * len(p)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and p[order[j + 1]] == p[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    pos_rank_sum = sum(r for r, label in zip(ranks, y) if label == 1)
    return (pos_rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def confusion_at(y: Sequence[int], p: Sequence[float], threshold: float) -> dict[str, int]:
    """Counts with ``p >= threshold`` as the positive call."""
    tp = sum(1 for label, q in zip(y, p) if q >= threshold and label == 1)
    fp = sum(1 for label, q in zip(y, p) if q >= threshold and label == 0)
    fn = sum(1 for label, q in zip(y, p) if q < threshold and label == 1)
    tn = sum(1 for label, q in zip(y, p) if q < threshold and label == 0)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def precision_recall_at(
    y: Sequence[int], p: Sequence[float], threshold: float,
) -> tuple[float | None, float | None]:
    c = confusion_at(y, p, threshold)
    precision = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None
    recall = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None
    return precision, recall


def threshold_for_recall(
    y: Sequence[int], p: Sequence[float], min_recall: float = MIN_RECALL,
) -> float | None:
    """Highest threshold whose recall is still >= ``min_recall``.

    Missing relevant news is worse than letting some noise through, so the
    operating point is set by recall and precision is whatever it then is.
    """
    if sum(y) == 0:
        return None
    for t in sorted(set(p), reverse=True):
        _, recall = precision_recall_at(y, p, t)
        if recall is not None and recall >= min_recall:
            return t
    return None


def brier(y: Sequence[int], p: Sequence[float]) -> float | None:
    if not y:
        return None
    return sum((q - label) ** 2 for label, q in zip(y, p)) / len(y)


def reliability_by_decile(y: Sequence[int], p: Sequence[float]) -> list[dict[str, Any]]:
    """Per probability decile: n, mean predicted, observed relevant rate."""
    bins: list[list[tuple[int, float]]] = [[] for _ in range(10)]
    for label, q in zip(y, p):
        bins[min(int(q * 10), 9)].append((label, q))
    out = []
    for i, members in enumerate(bins):
        n = len(members)
        out.append({
            "bin": f"{i / 10:.1f}-{(i + 1) / 10:.1f}",
            "n": n,
            "mean_p": sum(q for _, q in members) / n if n else None,
            "observed": sum(label for label, _ in members) / n if n else None,
        })
    return out


def share_near_half(p: Sequence[float], width: float = 0.1) -> float | None:
    if not p:
        return None
    return sum(1 for q in p if abs(q - 0.5) < width) / len(p)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, ``q`` in 0..100."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class ArmResult:
    name: str
    auc: float | None
    cost_per_1k: float | None
    failure_rate: float
    threshold: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def decide_judge(arms: dict[str, ArmResult]) -> tuple[str, str]:
    """Apply the spec's fixed decision rule. Returns (verdict, reason).

    verdict is ``jev``, ``haiku``, ``stop`` or ``undecided``.
    """
    prior = arms.get("prior")
    haiku = arms.get("haiku")
    jev = arms.get("jev")
    if prior is None or prior.auc is None or haiku is None or haiku.auc is None:
        return "undecided", "needs the prior and haiku arms with a computable AUC"

    if jev is not None and jev.auc is not None:
        if jev.cost_per_1k is None or haiku.cost_per_1k is None:
            return "undecided", "jev arm ran but a cost per 1k links is missing (pass prices)"
        auc_ok = jev.auc >= haiku.auc - JEV_MAX_AUC_GAP
        cost_ok = jev.cost_per_1k <= JEV_MAX_COST_RATIO * haiku.cost_per_1k
        fail_ok = jev.failure_rate < JEV_MAX_FAILURE_RATE
        if auc_ok and cost_ok and fail_ok:
            return "jev", (
                f"jev AUC {jev.auc:.3f} >= haiku {haiku.auc:.3f} - {JEV_MAX_AUC_GAP}, "
                f"cost ${jev.cost_per_1k:.4f} <= {JEV_MAX_COST_RATIO:.0%} of "
                f"${haiku.cost_per_1k:.4f}, failures {jev.failure_rate:.1%}"
            )
        failed = [name for name, ok in
                  (("AUC", auc_ok), ("cost", cost_ok), ("failure rate", fail_ok)) if not ok]
        jev_note = f"jev rejected on {', '.join(failed)}; "
    else:
        jev_note = "jev not evaluated; "

    if haiku.auc >= prior.auc + MIN_AUC_GAIN_OVER_PRIOR:
        return "haiku", (
            f"{jev_note}haiku AUC {haiku.auc:.3f} beats prior {prior.auc:.3f} "
            f"by >= {MIN_AUC_GAIN_OVER_PRIOR}"
        )
    return "stop", (
        f"{jev_note}haiku AUC {haiku.auc:.3f} does not beat prior {prior.auc:.3f} by "
        f"{MIN_AUC_GAIN_OVER_PRIOR}: weight sentiment_daily by the prior and raise "
        "the classification cap instead"
    )


# ---------- sample -----------------------------------------------------------


_SAMPLE_SQL = """
    WITH links AS (
        SELECT ac.article_id, ac.ticker, ac.relevance AS prior, a.source,
               a.title, left(a.body, 1000) AS body_excerpt,
               CASE WHEN ac.relevance >= 0.95 THEN '1.0'
                    WHEN ac.relevance >= 0.5  THEN '0.6'
                    ELSE '0.3' END AS stratum
        FROM article_companies ac
        JOIN articles a ON a.id = ac.article_id
        WHERE a.published_at >= now() - make_interval(days => %(days)s)
          AND ac.relevance IS NOT NULL
          AND ac.ticker = ANY(%(tickers)s)
    ), spread AS (
        SELECT *,
               row_number() OVER (PARTITION BY stratum, ticker ORDER BY random()) AS rn_ticker,
               row_number() OVER (PARTITION BY stratum, source ORDER BY random()) AS rn_source
        FROM links
    ), ranked AS (
        SELECT *,
               row_number() OVER (PARTITION BY stratum ORDER BY rn_source, random()) AS rn
        FROM spread
        WHERE rn_ticker <= 3
    )
    SELECT article_id, ticker, stratum, source, prior, title, body_excerpt
    FROM ranked
    WHERE rn <= %(per_stratum)s
    ORDER BY stratum, rn
"""

_HARD_SQL = """
    SELECT ac.article_id, ac.ticker, 'hard', a.source, ac.relevance,
           a.title, left(a.body, 1000)
    FROM article_companies ac
    JOIN articles a ON a.id = ac.article_id
    WHERE ac.article_id = %s AND ac.ticker = %s
"""


def _company_names() -> dict[str, str]:
    with UNIVERSE_PATH.open(newline="", encoding="utf-8") as fh:
        return {
            row["ticker"]: row["company_name"]
            for row in csv.DictReader(fh, delimiter=";")
            if row.get("ticker") and row.get("company_name")
        }


def cmd_sample(args: argparse.Namespace) -> int:
    from research_commons.db_news.connection import get_connection

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} exists — it may hold labels. Use --force to overwrite.", file=sys.stderr)
        return 1
    names = _company_names()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SAMPLE_SQL, {
                "days": args.days, "per_stratum": args.per_stratum, "tickers": sorted(names),
            })
            rows = list(cur.fetchall())
            for spec in args.hard:
                article_id, _, ticker = spec.partition(":")
                cur.execute(_HARD_SQL, (int(article_id), ticker))
                row = cur.fetchone()
                if row is None:
                    print(f"hard case {spec}: no such link", file=sys.stderr)
                else:
                    rows.append(row)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        for article_id, ticker, stratum, source, prior, title, body in rows:
            writer.writerow({
                "article_id": article_id, "ticker": ticker,
                "company_name": names.get(ticker, ""), "stratum": stratum,
                "source": source, "prior": prior, "title": title or "",
                "body_excerpt": body or "", "label": "", "notes": "",
            })
    print(f"wrote {len(rows)} links to {out} — fill `label` with relevant / "
          f"not_relevant / unsure")
    return 0


# ---------- run --------------------------------------------------------------


def _price(tokens_in: int, tokens_out: int, price_in: float | None,
           price_out: float | None, n_links: int) -> float | None:
    if price_in is None or price_out is None or not n_links:
        return None
    usd = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    return usd / n_links * 1000


def _fmt(value: float | None, spec: str = ".3f") -> str:
    return "—" if value is None else format(value, spec)


def cmd_run(args: argparse.Namespace) -> int:
    from research_commons.db_news.relevance import (
        HaikuRelevanceJudge,
        JevNotEnabledError,
        JevRelevanceJudge,
        RelevanceJudge,
        RelevanceLink,
    )

    with Path(args.labels).open(newline="", encoding="utf-8") as fh:
        all_rows = list(csv.DictReader(fh))
    unsure = sum(1 for r in all_rows if r["label"].strip() == "unsure")
    unlabelled = sum(1 for r in all_rows if not r["label"].strip())
    rows = [r for r in all_rows if r["label"].strip() in LABEL_VALUES]
    if not rows:
        print("no rows labelled relevant / not_relevant", file=sys.stderr)
        return 1
    y = [LABEL_VALUES[r["label"].strip()] for r in rows]
    priors = [float(r["prior"]) if r["prior"] else 0.0 for r in rows]
    strata = [r["stratum"] for r in rows]
    links = [
        RelevanceLink(int(r["article_id"]), r["ticker"], r["company_name"],
                      r["title"], r["body_excerpt"])
        for r in rows
    ]

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    probs: dict[str, list[float]] = {}
    results: dict[str, ArmResult] = {}
    notes: list[str] = []
    for arm in arm_names:
        if arm == "prior":
            probs[arm] = priors
            results[arm] = ArmResult("prior", roc_auc(y, priors), 0.0, 0.0)
            continue
        judge: RelevanceJudge
        if arm == "haiku":
            judge = HaikuRelevanceJudge()
            price_in, price_out = args.haiku_price_in, args.haiku_price_out
        elif arm == "jev":
            try:
                judge = JevRelevanceJudge()
            except JevNotEnabledError as exc:
                notes.append(f"jev arm skipped: {exc}")
                continue
            price_in, price_out = args.jev_price_in, args.jev_price_out
        else:
            print(f"unknown arm {arm!r}", file=sys.stderr)
            return 1

        judgments = judge.judge(links)
        by_key = {(j.article_id, j.ticker): j.p_relevant for j in judgments}
        raw = [by_key.get((link.article_id, link.ticker)) for link in links]
        failures = sum(1 for q in raw if q is None)
        # A failed judgment falls back to the prior, exactly as in production.
        effective = [q if q is not None else prior for q, prior in zip(raw, priors)]
        probs[arm] = effective
        usage = judge.usage
        results[arm] = ArmResult(
            arm, roc_auc(y, effective),
            _price(usage.input_tokens, usage.output_tokens, price_in, price_out, len(links)),
            failures / len(links),
            extras={
                "p50_ms": percentile(usage.latencies_ms, 50),
                "p95_ms": percentile(usage.latencies_ms, 95),
                "calls": usage.calls,
            },
        )

    for name, res in results.items():
        res.threshold = threshold_for_recall(y, probs[name])

    verdict, reason = decide_judge(results)
    report = _render_report(
        args, all_rows=len(all_rows), unsure=unsure, unlabelled=unlabelled, rows=rows,
        y=y, strata=strata, probs=probs, results=results, verdict=verdict,
        reason=reason, notes=notes,
    )
    out = Path(args.out or EVAL_DIR / f"report_{date.today():%Y%m%d}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"verdict: {verdict} — {reason}\nreport: {out}")
    return 0


def _render_report(
    args: argparse.Namespace, *, all_rows: int, unsure: int, unlabelled: int,
    rows: list[dict[str, str]], y: list[int], strata: list[str],
    probs: dict[str, list[float]], results: dict[str, ArmResult],
    verdict: str, reason: str, notes: list[str],
) -> str:
    lines = [
        f"# Relevance judge eval — {date.today():%Y-%m-%d}",
        "",
        (f"Labelled set: {all_rows} rows; {len(rows)} used, {unsure} unsure (excluded), "
         f"{unlabelled} unlabelled. Relevant: {sum(y)}."),
        "",
        f"**Verdict: {verdict}** — {reason}",
        "",
    ]
    lines += [f"- {n}" for n in notes]
    lines += [
        "",
        "## Metrics",
        "",
        (f"Threshold = highest value keeping recall >= {MIN_RECALL:.2f}. "
         "Failed judgments fall back to the prior."),
        "",
        ("| arm | AUC | threshold | precision | recall | Brier | near 0.5 | failures "
         "| p50 ms | p95 ms | $ / 1k links |"),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, res in results.items():
        p = probs[name]
        precision, recall = (
            precision_recall_at(y, p, res.threshold) if res.threshold is not None
            else (None, None)
        )
        lines.append(
            f"| {name} | {_fmt(res.auc)} | {_fmt(res.threshold, '.2f')} | "
            f"{_fmt(precision)} | {_fmt(recall)} | {_fmt(brier(y, p))} | "
            f"{_fmt(share_near_half(p), '.1%')} | {res.failure_rate:.1%} | "
            f"{_fmt(res.extras.get('p50_ms'), '.0f')} | {_fmt(res.extras.get('p95_ms'), '.0f')} | "
            f"{_fmt(res.cost_per_1k, '.4f')} |"
        )

    lines += ["", "## Per stratum (AUC / precision / recall at the arm's threshold)", ""]
    stratum_names = sorted(set(strata))
    lines += ["| arm | " + " | ".join(stratum_names) + " |",
              "|---|" + "---|" * len(stratum_names)]
    for name, res in results.items():
        cells = []
        for s in stratum_names:
            idx = [i for i, st in enumerate(strata) if st == s]
            ys = [y[i] for i in idx]
            ps = [probs[name][i] for i in idx]
            pr = (precision_recall_at(ys, ps, res.threshold) if res.threshold is not None
                  else (None, None))
            cells.append(f"n={len(idx)} {_fmt(roc_auc(ys, ps))} / {_fmt(pr[0])} / {_fmt(pr[1])}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    for name, res in results.items():
        p = probs[name]
        lines += ["", f"## {name}", "", "Reliability by decile:", "",
                  "| bin | n | mean p | observed |", "|---|---|---|---|"]
        for b in reliability_by_decile(y, p):
            lines.append(f"| {b['bin']} | {b['n']} | {_fmt(b['mean_p'])} | {_fmt(b['observed'])} |")
        if res.threshold is None:
            continue
        c = confusion_at(y, p, res.threshold)
        lines += [
            "", f"Confusion at threshold {res.threshold:.2f}:", "",
            "| | labelled relevant | labelled not relevant |", "|---|---|---|",
            f"| judged relevant | {c['tp']} | {c['fp']} |",
            f"| judged not relevant | {c['fn']} | {c['tn']} |",
        ]
        if name == "prior":
            continue
        disagreements = [i for i in range(len(y)) if (p[i] >= res.threshold) != bool(y[i])]
        sample = random.Random(args.seed).sample(disagreements, min(20, len(disagreements)))
        lines += ["", f"Disagreements ({len(disagreements)} total, {len(sample)} shown):", "",
                  "| label | p | stratum | ticker | title |", "|---|---|---|---|---|"]
        for i in sorted(sample):
            r = rows[i]
            title = r["title"].replace("|", "/")[:120]
            lines.append(f"| {r['label']} | {p[i]:.2f} | {r['stratum']} | {r['ticker']} | {title} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("sample", help="draw a stratified sample for labelling")
    sp.add_argument("--days", type=int, default=60)
    sp.add_argument("--per-stratum", type=int, default=100)
    sp.add_argument("--hard", action="append", default=[], metavar="ARTICLE_ID:TICKER",
                    help="append a hand-picked hard case (repeatable)")
    sp.add_argument("--out", default=str(LABELS_PATH))
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_sample)

    rp = sub.add_parser("run", help="score the labelled set with each arm")
    rp.add_argument("--labels", default=str(LABELS_PATH))
    rp.add_argument("--arms", default="prior,haiku")
    rp.add_argument("--haiku-price-in", type=float, default=1.0, help="USD per Mtok")
    rp.add_argument("--haiku-price-out", type=float, default=5.0, help="USD per Mtok")
    rp.add_argument("--jev-price-in", type=float, default=None, help="USD per Mtok")
    rp.add_argument("--jev-price-out", type=float, default=None, help="USD per Mtok")
    rp.add_argument("--seed", type=int, default=7)
    rp.add_argument("--out", default=None)
    rp.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
