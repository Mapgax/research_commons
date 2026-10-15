#!/usr/bin/env python3
"""Offline eval for the relevance gate (docs/spec-jev-relevance-gate.md, Phase 2).

Two steps, then a monthly loop (docs/relevance-gate-runbook.md):

    # 1. draw a stratified sample of real links into a CSV for hand-labelling
    python3 scripts/relevance_eval.py sample
    python3 scripts/relevance_eval.py sample --hard 123456:AAPL --hard 123789:GE

    # 2. after filling the `label` column (relevant / not_relevant / unsure)
    python3 scripts/relevance_eval.py run --arms prior,haiku
    python3 scripts/relevance_eval.py run --arms prior,haiku,jev \\
        --jev-price-in <usd/Mtok> --jev-price-out <usd/Mtok>

    # monthly: append ~50 fresh links (never one already in labels.csv), label
    # them, re-run — each run appends one row per arm to history.csv
    python3 scripts/relevance_eval.py sample --top-up 50
    python3 scripts/relevance_eval.py run

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
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "eval" / "relevance"
LABELS_PATH = EVAL_DIR / "labels.csv"
HISTORY_PATH = EVAL_DIR / "history.csv"
UNIVERSE_PATH = REPO_ROOT / "data" / "ticker_universe.csv"

LABEL_COLUMNS = [
    "article_id", "ticker", "company_name", "stratum", "source", "prior",
    "title", "body_excerpt", "label", "notes", "sampled_on",
]
LABEL_VALUES = {"relevant": 1, "not_relevant": 0}

MIN_RECALL = 0.90
STRATA = ("0.3", "0.6", "1.0")
DEFAULT_SAMPLE_DAYS = 60
DEFAULT_TOP_UP_DAYS = 35       # top-ups track the current news mix
HISTORY_COLUMNS = [
    "run_date", "arm", "judge_version", "n_used", "n_relevant", "n_unsure",
    "auc", "brier", "failure_rate", "cost_per_1k",
    "threshold_r90", "precision_r90", "recall_r90",
    "prod_threshold", "precision_prod", "recall_prod", "verdict",
]

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


# ---------- labelled-set and history helpers (pure) --------------------------


def labelled_keys(rows: Sequence[dict[str, str]]) -> set[tuple[int, str]]:
    """(article_id, ticker) of every row already in the labels file, labelled or not."""
    return {(int(r["article_id"]), r["ticker"]) for r in rows if r.get("article_id")}


def balanced_take(rows: Sequence[Sequence[Any]], n: int, *, stratum_index: int = 2) -> list[Any]:
    """Up to ``n`` rows, round-robin across strata in :data:`STRATA` order.

    Rows within a stratum keep their order (the SQL already randomised it). A
    stratum that runs dry leaves its share to the others.
    """
    by_stratum: dict[str, list[Any]] = {}
    for row in rows:
        by_stratum.setdefault(str(row[stratum_index]), []).append(row)
    order = [s for s in STRATA if s in by_stratum] + sorted(set(by_stratum) - set(STRATA))
    queues = [list(by_stratum[s]) for s in order]
    out: list[Any] = []
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n:
                out.append(q.pop(0))
    return out


def _cell(value: float | None, spec: str = ".4f") -> str:
    return "" if value is None else format(value, spec)


def history_rows(
    *, run_date: date, results: dict[str, ArmResult], probs: dict[str, list[float]],
    y: Sequence[int], versions: dict[str, str], prod_threshold: float, n_unsure: int,
    verdict: str,
) -> list[dict[str, str]]:
    """One history row per arm: quality at the recall-0.90 operating point and
    at the threshold production actually uses."""
    out = []
    for name, res in results.items():
        p = probs[name]
        p90, r90 = (precision_recall_at(y, p, res.threshold) if res.threshold is not None
                    else (None, None))
        pp, rp = precision_recall_at(y, p, prod_threshold)
        out.append({
            "run_date": run_date.isoformat(), "arm": name,
            "judge_version": versions.get(name, ""), "n_used": str(len(y)),
            "n_relevant": str(sum(y)), "n_unsure": str(n_unsure),
            "auc": _cell(res.auc), "brier": _cell(brier(y, p)),
            "failure_rate": _cell(res.failure_rate), "cost_per_1k": _cell(res.cost_per_1k),
            "threshold_r90": _cell(res.threshold, ".3f"),
            "precision_r90": _cell(p90), "recall_r90": _cell(r90),
            "prod_threshold": _cell(prod_threshold, ".3f"),
            "precision_prod": _cell(pp), "recall_prod": _cell(rp), "verdict": verdict,
        })
    return out


def read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def append_history(path: Path, rows: Sequence[dict[str, str]]) -> None:
    """Append rows, writing the header only when the file is new."""
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_COLUMNS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerows(rows)


def render_history(rows: Sequence[dict[str, str]], *, last: int = 12) -> list[str]:
    """Markdown table of the last ``last`` runs, one line per (run, arm)."""
    if not rows:
        return []
    dates = sorted({r["run_date"] for r in rows})[-last:]
    keep = [r for r in rows if r["run_date"] in dates]
    lines = [
        ("| run | arm | judge | n | AUC | P@r90 | R@r90 | thr r90 | P@prod | R@prod | thr prod "
         "| failures |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in keep:
        lines.append(
            f"| {r['run_date']} | {r['arm']} | {r.get('judge_version') or '—'} | "
            f"{r['n_used']} | {r['auc'] or '—'} | {r['precision_r90'] or '—'} | "
            f"{r['recall_r90'] or '—'} | {r['threshold_r90'] or '—'} | "
            f"{r['precision_prod'] or '—'} | {r['recall_prod'] or '—'} | "
            f"{r['prod_threshold'] or '—'} | {r['failure_rate'] or '—'} |"
        )
    return lines


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
          AND (ac.article_id, ac.ticker) NOT IN (
              SELECT * FROM unnest(%(ex_ids)s::bigint[], %(ex_tickers)s::text[]))
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


def _read_labels(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return list(reader.fieldnames or LABEL_COLUMNS), rows


def cmd_sample(args: argparse.Namespace) -> int:
    from research_commons.db_news.connection import get_connection

    out = Path(args.out)
    top_up = args.top_up is not None
    fieldnames, existing = LABEL_COLUMNS, []
    if top_up:
        if args.top_up <= 0:
            print("--top-up needs a positive count", file=sys.stderr)
            return 1
        if out.exists():
            fieldnames, existing = _read_labels(out)
            for col in LABEL_COLUMNS:  # older files predate sampled_on
                if col not in fieldnames:
                    fieldnames.append(col)
    elif out.exists() and not args.force:
        print(f"{out} exists — it may hold labels. Use --top-up N to add to it, or "
              "--force to overwrite.", file=sys.stderr)
        return 1

    exclude = labelled_keys(existing)
    days = args.days or (DEFAULT_TOP_UP_DAYS if top_up else DEFAULT_SAMPLE_DAYS)
    per_stratum = math.ceil(args.top_up / len(STRATA)) if top_up else args.per_stratum
    names = _company_names()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(_SAMPLE_SQL, {
            "days": days, "per_stratum": per_stratum, "tickers": sorted(names),
            "ex_ids": [k[0] for k in sorted(exclude)],
            "ex_tickers": [k[1] for k in sorted(exclude)],
        })
        rows = list(cur.fetchall())
        if top_up:
            rows = balanced_take(rows, args.top_up)
        for spec in args.hard:
            article_id, _, ticker = spec.partition(":")
            if (int(article_id), ticker) in exclude:
                print(f"hard case {spec}: already in {out}", file=sys.stderr)
                continue
            cur.execute(_HARD_SQL, (int(article_id), ticker))
            row = cur.fetchone()
            if row is None:
                print(f"hard case {spec}: no such link", file=sys.stderr)
            else:
                rows.append(row)

    today = date.today().isoformat()
    new_rows = [
        {
            "article_id": article_id, "ticker": ticker,
            "company_name": names.get(ticker, ""), "stratum": stratum,
            "source": source, "prior": prior, "title": title or "",
            "body_excerpt": body or "", "label": "", "notes": "", "sampled_on": today,
        }
        for article_id, ticker, stratum, source, prior, title, body in rows
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(new_rows)
    tmp.replace(out)  # an interrupted write never truncates the labels
    strata = {s: sum(1 for r in new_rows if r["stratum"] == s) for s in sorted(
        {r["stratum"] for r in new_rows})}
    print(f"{'added' if top_up else 'wrote'} {len(new_rows)} links to {out} "
          f"(last {days} days; {strata}) — fill `label` with relevant / not_relevant / unsure")
    if top_up and len(new_rows) < args.top_up:
        print(f"only {len(new_rows)} unlabelled links found — widen --days", file=sys.stderr)
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
    from research_commons.config import get_settings
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
    versions: dict[str, str] = {"prior": "prior"}
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
        # The full version string carries the model, so a fallback run shows.
        answered = Counter(j.judge_version for j in judgments if j.p_relevant is not None)
        versions[arm] = answered.most_common(1)[0][0] if answered else judge.version
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
    prod_threshold = (get_settings().relevance_threshold if args.threshold is None
                      else args.threshold)
    history_path = Path(args.history)
    history = read_history(history_path)
    if not args.no_history:
        new_rows = history_rows(
            run_date=date.today(), results=results, probs=probs, y=y, versions=versions,
            prod_threshold=prod_threshold, n_unsure=unsure, verdict=verdict,
        )
        append_history(history_path, new_rows)
        history += new_rows
    report = _render_report(
        args, all_rows=len(all_rows), unsure=unsure, unlabelled=unlabelled, rows=rows,
        y=y, strata=strata, probs=probs, results=results, verdict=verdict,
        reason=reason, notes=notes, prod_threshold=prod_threshold, history=history,
    )
    out = Path(args.out or EVAL_DIR / f"report_{date.today():%Y%m%d}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"verdict: {verdict} — {reason}\nreport: {out}")
    if not args.no_history:
        print(f"history: {history_path}")
    return 0


def _render_report(
    args: argparse.Namespace, *, all_rows: int, unsure: int, unlabelled: int,
    rows: list[dict[str, str]], y: list[int], strata: list[str],
    probs: dict[str, list[float]], results: dict[str, ArmResult],
    verdict: str, reason: str, notes: list[str], prod_threshold: float,
    history: Sequence[dict[str, str]],
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

    lines += [
        "",
        f"## At the production threshold (RELEVANCE_THRESHOLD = {prod_threshold:.2f})",
        "",
        "| arm | precision | recall | share passing |",
        "|---|---|---|---|",
    ]
    for name in results:
        p = probs[name]
        precision, recall = precision_recall_at(y, p, prod_threshold)
        passing = sum(1 for q in p if q >= prod_threshold) / len(p)
        lines.append(f"| {name} | {_fmt(precision)} | {_fmt(recall)} | {passing:.1%} |")
    if history:
        lines += ["", "## Over time (history.csv, last 12 runs)", ""] + render_history(history)

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
    sp.add_argument("--days", type=int, default=None,
                    help=f"look-back (default {DEFAULT_SAMPLE_DAYS}, "
                         f"{DEFAULT_TOP_UP_DAYS} with --top-up)")
    sp.add_argument("--per-stratum", type=int, default=100)
    sp.add_argument("--top-up", type=int, default=None, metavar="N",
                    help="append N new links, balanced across strata, skipping any "
                         "already in the labels file")
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
    rp.add_argument("--threshold", type=float, default=None,
                    help="production threshold to report at (default RELEVANCE_THRESHOLD)")
    rp.add_argument("--history", default=str(HISTORY_PATH))
    rp.add_argument("--no-history", action="store_true",
                    help="don't append this run to the history file")
    rp.add_argument("--out", default=None)
    rp.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
