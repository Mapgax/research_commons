"""CLI for the central ticker-universe workflow.

Usage:
    python3 -m research_commons.universe ingest <file> --mode universe|focus
        [--apply] [--yes] [--new-status watchlist|inactive]
        [--resolve ISIN=TICKER ...]
    python3 -m research_commons.universe verify
    python3 -m research_commons.universe distribute [--apply]
    python3 -m research_commons.universe sync-db
    python3 -m research_commons.universe rename-ticker OLD NEW [--apply]
    python3 -m research_commons.universe update-email-focus [file] [--apply]

``ingest`` is plan-only by default: it prints the full merge report (adds,
promotions/demotions, quarantined issues, cost delta) and writes nothing.
``--apply`` executes: canonical file -> DB mirror -> consumer distribution ->
archive + report, aborting on the first failure.

Update-file naming convention (checked against --mode):
    universe_update_YYYYMMDD[_HHMM].xlsx|csv   (general, additive)
    focus_update_YYYYMMDD[_HHMM].xlsx|csv      (focus snapshot)

``update-email-focus`` merges a PM/advisory audience upload (default: most
recent file in universe_update_xlsx_files/) into data/email_focus_tickers.csv
-- a second, independent dial from scope_status that governs who gets the
briefing email, not what gets collected. See email_focus.py for the merge
semantics and CLAUDE.md's "Email-focus audience" section for the full picture.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from research_commons.universe.dist import TargetResult
    from research_commons.universe.email_focus import EmailFocusRow
    from research_commons.universe.io import UniverseRow

# Guardrails: more activations/demotions than this require an explicit --yes.
ACTIVATION_THRESHOLD = 20
DEMOTION_THRESHOLD = 5


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _check_filename_mode(path: Path, mode: str) -> str:
    stem = path.stem.lower()
    if stem.startswith("universe_update") and mode != "universe":
        return f"{path.name} is named as a universe update but --mode is {mode!r}"
    if stem.startswith("focus_update") and mode != "focus":
        return f"{path.name} is named as a focus update but --mode is {mode!r}"
    return ""


def _parse_resolutions(pairs: list[str]) -> dict[str, str]:
    resolutions: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--resolve expects ISIN=TICKER, got {pair!r}")
        isin, _, ticker = pair.partition("=")
        resolutions[isin.strip().upper()] = ticker.strip().upper()
    return resolutions


def _publish(
    results: list[TargetResult], filename: str, summary: str, *, no_push: bool
) -> bool:
    """Commit and push *filename* in every written target; True if all landed.

    ``--no-push`` prints the manual commands instead and reports success —
    nothing was attempted, so nothing failed.
    """
    from research_commons.universe.dist import commit_instructions, push_targets

    print()
    if no_push:
        print(commit_instructions(results, summary, filename=filename))
        return True
    for result in push_targets(results, filename, summary):
        print(f"  {'✓' if result.pushed else '✗'} {result.target.name}: {result.push_message}")
    # Every target, not just the written ones: an all-or-nothing distribute can
    # hand back a set where nothing is ok, and "all zero of them pushed" must
    # not read as success.
    return all(r.pushed for r in results)


def _apply_canonical(
    rows: list[UniverseRow],
    *,
    current_rows: list[UniverseRow],
) -> list[TargetResult] | None:
    """Write *rows* everywhere the canonical universe is mirrored, or fail.

    Order matters and is the same for every writer (``ingest``, and the
    email-focus promotion): check the consumer repos and the DB schema BEFORE
    touching anything, then canonical file -> DB mirror -> consumer tickers.csv.
    Returns the distribution results, or None once it has reported the failure.
    """
    from research_commons.universe import canonical_path, targets_path
    from research_commons.universe.db import preflight_schema, sync_canonical_to_db
    from research_commons.universe.dist import distribute, load_targets
    from research_commons.universe.io import write_canonical

    targets = load_targets(targets_path())
    blocked = [r for r in distribute(targets, rows, apply=False, current_rows=current_rows) if not r.ok]
    if blocked:
        for result in blocked:
            print(f"  ✗ {result.target.name}: {result.message}", file=sys.stderr)
        _fail("distribution target check failed — nothing was written")
        return None

    schema_problems = preflight_schema()
    if schema_problems:
        for problem in schema_problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        _fail(
            "live DB schema does not match migration 0003 — run "
            "python3 -c 'import research_commons; research_commons.init_market_db()'"
        )
        return None

    write_canonical(canonical_path(), rows)
    print(f"✓ canonical updated: {canonical_path()}")

    # Everything past this point runs with the canonical file already updated,
    # so every failure has to say how to get the DB and the repos back in line.
    try:
        count = sync_canonical_to_db(rows)
        print(f"✓ DB mirror synced: {count} rows upserted into ticker_metadata")

        target_results = distribute(targets, rows, apply=True, current_rows=current_rows)
        for result in target_results:
            print(f"  {'✓' if result.ok else '✗'} {result.target.name}: {result.message}")
        if any(not result.ok for result in target_results):
            failed = "; ".join(f"{r.target.name}: {r.message}" for r in target_results if not r.ok)
            raise RuntimeError(f"distribution did not write: {failed}")
    except Exception as exc:
        traceback.print_exc()
        print(
            f"ERROR: apply failed after the canonical file was written: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        print(
            "The canonical file is updated; the DB mirror and/or the consumer repos "
            "are not.\nRecover with:\n"
            "  python3 -m research_commons.universe sync-db\n"
            "  python3 -m research_commons.universe distribute --apply",
            file=sys.stderr,
        )
        return None
    return target_results


def cmd_ingest(args: argparse.Namespace) -> int:
    from research_commons.universe import archive_dir, canonical_path, ignore_path, reports_dir
    from research_commons.universe.io import read_canonical, read_ignore_list, read_update_file
    from research_commons.universe.merge import plan_focus_merge, plan_universe_merge
    from research_commons.universe.report import render_report

    source = Path(args.file).expanduser()
    if not source.exists():
        return _fail(f"update file not found: {source}")
    mismatch = _check_filename_mode(source, args.mode)
    if mismatch:
        return _fail(mismatch + " — aborting (rename the file or fix --mode)")

    try:
        resolutions = _parse_resolutions(args.resolve or [])
    except ValueError as exc:
        return _fail(str(exc))

    canonical = read_canonical(canonical_path())
    ignore = read_ignore_list(ignore_path())
    try:
        updates = read_update_file(source)
    except (RuntimeError, ValueError) as exc:
        return _fail(str(exc))
    if not updates:
        return _fail(f"no data rows found in {source.name}")

    if args.mode == "universe":
        plan = plan_universe_merge(
            canonical,
            updates,
            ignore,
            new_status=args.new_status,
            resolutions=resolutions,
        )
        activations = len(plan.adds) if args.new_status != "watchlist" else 0
        demotions = 0
    else:
        plan = plan_focus_merge(canonical, updates, ignore)
        activations = len(plan.promotions)
        demotions = len(plan.demotions)

    now = datetime.now()
    report_text = render_report(
        plan,
        source_name=source.name,
        applied=False,
        now=now,
        new_status=args.new_status,
    )

    if not args.apply:
        print(report_text)
        print("Plan only — nothing written. Re-run with --apply to execute.")
        return 0

    # ---- apply gates -------------------------------------------------------
    if plan.quarantined and not args.allow_quarantine:
        print(report_text)
        return _fail(
            f"{len(plan.quarantined)} quarantined issue(s) — resolve them (see "
            "suggestions), or pass --allow-quarantine to apply the clean part "
            "and leave the quarantined rows out."
        )
    if activations > ACTIVATION_THRESHOLD and not args.yes:
        return _fail(
            f"{activations} names would enter covered/active state "
            f"(> {ACTIVATION_THRESHOLD}); re-run with --yes to confirm the cost."
        )
    if demotions > DEMOTION_THRESHOLD and not args.yes:
        return _fail(
            f"{demotions} active names would be demoted (> {DEMOTION_THRESHOLD}); "
            "if this focus file is really complete, re-run with --yes."
        )

    target_results = _apply_canonical(plan.result, current_rows=canonical)
    if target_results is None:
        return 1

    stamp = now.strftime("%H%M%S")
    year_dir = archive_dir() / f"{now:%Y}"
    year_dir.mkdir(parents=True, exist_ok=True)
    archived = year_dir / f"{source.stem}_{stamp}{source.suffix.lower()}"
    shutil.copy2(source, archived)
    print(f"✓ input archived: {archived}")

    reports_dir().mkdir(parents=True, exist_ok=True)
    report_path = reports_dir() / f"{now:%Y%m%d}_{stamp}_{plan.mode}.md"
    report_text = render_report(
        plan, source_name=source.name, applied=True, now=now, new_status=args.new_status
    )
    report_path.write_text(report_text, encoding="utf-8")
    print(f"✓ report written: {report_path}")

    if plan.mode == "universe":
        summary = f"universe: ingest {source.stem} (+{len(plan.adds)} {args.new_status})"
    else:
        summary = (
            f"universe: focus update {source.stem} "
            f"(+{len(plan.promotions)} active, -{len(plan.demotions)})"
        )
    print()
    print(f"research_commons commit: {summary!r}")
    if not _publish(target_results, "tickers.csv", summary, no_push=args.no_push):
        print(
            "\nSome targets did not push automatically — see messages above "
            "and push manually where needed.",
            file=sys.stderr,
        )
    if plan.quarantined:
        print()
        print(
            f"NOTE: {len(plan.quarantined)} quarantined row(s) were left out — "
            f"see {report_path.name}."
        )
    return 0


def _report_delivery_scope_violations(canonical: list[UniverseRow]) -> list[UniverseRow]:
    """Print any emailed-but-unscored names and return them."""
    from research_commons.universe import email_focus_path
    from research_commons.universe.email_focus import delivery_scope_violations, read_email_focus

    violations = delivery_scope_violations(canonical, read_email_focus(email_focus_path()))
    for row in violations:
        print(f"  ✗ {row.ticker} ({row.isin}) {row.company_name}: "
              f"email_scope=1 but scope_status={row.scope_status}")
    return violations


def cmd_verify(_args: argparse.Namespace) -> int:
    from research_commons.universe import canonical_path
    from research_commons.universe.db import drift_check, preflight_schema
    from research_commons.universe.io import read_canonical

    problems = preflight_schema()
    if problems:
        for problem in problems:
            print(f"  ✗ schema: {problem}", file=sys.stderr)
        return 1

    canonical = read_canonical(canonical_path())
    violations = _report_delivery_scope_violations(canonical)
    if violations:
        print(
            f"{len(violations)} emailed name(s) are not active — their risk numbers "
            "refresh weekly, not daily. Fix with update-email-focus --apply."
        )

    report = drift_check(canonical)
    if report.clean:
        print("✓ canonical universe and ticker_metadata are in sync")
        return 1 if violations else 0
    for ticker in report.missing_in_db:
        print(f"  ✗ missing in DB: {ticker}")
    for ticker in report.extra_in_db:
        print(f"  ✗ in DB but not canonical: {ticker}")
    for ticker, diffs in report.mismatches.items():
        details = "; ".join(
            f"{field} canonical={a!r} db={b!r}" for field, (a, b) in diffs.items()
        )
        print(f"  ✗ {ticker}: {details}")
    print(
        f"Drift: {len(report.missing_in_db)} missing, {len(report.extra_in_db)} extra, "
        f"{len(report.mismatches)} mismatched — run sync-db (or investigate) "
    )
    return 1


def cmd_sync_db(_args: argparse.Namespace) -> int:
    from research_commons.universe import canonical_path
    from research_commons.universe.db import preflight_schema, sync_canonical_to_db
    from research_commons.universe.io import read_canonical

    problems = preflight_schema()
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return _fail("schema preflight failed — apply migrations first (init_market_db)")
    rows = read_canonical(canonical_path())
    count = sync_canonical_to_db(rows)
    print(f"✓ {count} rows upserted into ticker_metadata")
    return 0


def cmd_distribute(args: argparse.Namespace) -> int:
    from research_commons.universe import canonical_path, targets_path
    from research_commons.universe.dist import distribute, load_targets
    from research_commons.universe.io import read_canonical

    rows = read_canonical(canonical_path())
    targets = load_targets(targets_path())
    results = distribute(targets, rows, apply=args.apply, current_rows=rows)
    ok = True
    for result in results:
        marker = "✓" if result.ok else "✗"
        print(f"  {marker} {result.target.name}: {result.message}")
        ok = ok and result.ok
    if not ok:
        return 1
    if not args.apply:
        print("Plan only — pass --apply to write the files.")
        return 0
    summary = "universe: refresh tickers.csv from canonical"
    return 0 if _publish(results, "tickers.csv", summary, no_push=args.no_push) else 1


def cmd_rename_ticker(args: argparse.Namespace) -> int:
    from research_commons.universe import canonical_path, targets_path
    from research_commons.universe.db import preflight_schema
    from research_commons.universe.dist import distribute, load_targets
    from research_commons.universe.io import read_canonical, write_canonical

    old = args.old.strip().upper()
    new = args.new.strip().upper()
    rows = read_canonical(canonical_path())
    by_ticker = {r.ticker: r for r in rows}
    if old not in by_ticker:
        return _fail(f"unknown ticker: {old}")
    if new in by_ticker:
        return _fail(f"target ticker already exists: {new}")

    if not args.apply:
        print(f"Would rename {old} -> {new}:")
        print("  1. UPDATE ticker_metadata SET ticker = new (FKs cascade: prices, ")
        print("     fundamentals, features, alt_data, insider_transactions, option_metrics)")
        print("  2. update canonical universe file")
        print("  3. regenerate consumer tickers.csv files")
        print("Re-run with --apply to execute.")
        return 0

    problems = preflight_schema()
    if problems:
        return _fail("schema preflight failed — apply migrations first (init_market_db)")

    from research_commons.db_market.connection import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ticker_metadata SET ticker = %s, updated_at = now() "
                "WHERE ticker = %s",
                (new, old),
            )
            affected = cur.rowcount
            conn.commit()
    print(f"✓ DB: {affected} row renamed ({old} -> {new}), history cascaded")

    from dataclasses import replace

    pre_rename_rows = rows
    rows = [replace(r, ticker=new) if r.ticker == old else r for r in rows]
    write_canonical(canonical_path(), rows)
    print(f"✓ canonical updated: {canonical_path()}")

    targets = load_targets(targets_path())
    results = distribute(targets, rows, apply=True, current_rows=pre_rename_rows)
    for result in results:
        marker = "✓" if result.ok else "✗"
        print(f"  {marker} {result.target.name}: {result.message}")
    if any(not result.ok for result in results):
        return _fail(
            f"the DB and canonical file now say {new}, but no consumer repo was "
            f"written — they still request {old} against renamed history. "
            "Fix the targets above, then: distribute --apply"
        )
    summary = f"universe: rename {old} -> {new}"
    return 0 if _publish(results, "tickers.csv", summary, no_push=args.no_push) else 1


def _interactive_resolve_removed(row: EmailFocusRow) -> bool:
    """Prompt for an ISIN that dropped out of the latest email-focus upload.

    Defaults to "keep" (the least destructive answer) when stdin isn't a
    TTY, rather than blocking on input() forever.
    """
    if not sys.stdin.isatty():
        print(
            f"  ! {row.isin} ({row.company_name}) missing from upload, stdin is "
            f"not interactive — defaulting to KEEP (pm_ch={row.scope_pm_ch} "
            f"pm_de={row.scope_pm_de} email_scope={row.email_scope}); "
            "scope_advisory resets to 0 either way.",
            file=sys.stderr,
        )
        return True
    print(
        f"\n{row.isin}  {row.company_name}\n"
        f"  currently: scope_pm_ch={row.scope_pm_ch}  scope_pm_de={row.scope_pm_de}  "
        f"scope_advisory={row.scope_advisory}  email_scope={row.email_scope}\n"
        "  — missing from this upload. scope_advisory resets to 0 either way."
    )
    while True:
        answer = input("  keep pm/email at current values, or zero them? [k/z]: ").strip().lower()
        if answer in {"k", "keep"}:
            return True
        if answer in {"z", "zero"}:
            return False
        print("  please answer 'k' (keep) or 'z' (zero)")


def cmd_update_email_focus(args: argparse.Namespace) -> int:
    from research_commons.universe import canonical_path, email_focus_path, email_focus_update_dir, targets_path
    from research_commons.universe.dist import distribute_email_focus, load_targets
    from research_commons.universe.email_focus import (
        latest_update_file,
        plan_email_focus_merge,
        promote_delivery_scopes,
        read_email_focus,
        read_email_focus_upload,
        write_email_focus,
        _rows_to_csv,
    )
    from research_commons.universe.io import read_canonical

    if args.file:
        source = Path(args.file).expanduser()
    else:
        source = latest_update_file(email_focus_update_dir())
        if source is None:
            return _fail(
                f"no update file given and none found in {email_focus_update_dir()} "
                "(expected a .xlsx or .csv there, or pass a path explicitly)"
            )
        print(f"Using most recent upload: {source}")
    if not source.exists():
        return _fail(f"update file not found: {source}")

    try:
        upload_rows = read_email_focus_upload(source)
    except (RuntimeError, ValueError) as exc:
        return _fail(str(exc))
    if not upload_rows:
        return _fail(f"no data rows found in {source.name}")

    current_rows = read_email_focus(email_focus_path())
    plan = plan_email_focus_merge(current_rows, upload_rows, resolve_removed=_interactive_resolve_removed)

    print(f"\nAdded:   {len(plan.added)}")
    print(f"Updated: {len(plan.updated)}")
    print(f"Kept (removed from upload, pm/email unchanged, advisory->0): {len(plan.kept)}")
    print(f"Zeroed (removed from upload, all flags->0): {len(plan.zeroed)}")

    # Delivery and coverage are independent dials in every direction but one:
    # an emailed name is one the desk is working, so it must sit in the tier
    # that scores daily. Promote here rather than only reporting, so the
    # audience file can never leave the universe in a stale-numbers state.
    canonical = read_canonical(canonical_path())
    promoted_result, promoted = promote_delivery_scopes(canonical, plan.result)
    print(f"Promoted to active (email_scope=1, was not active): {len(promoted)}")
    for row in promoted:
        print(f"  → {row.ticker} ({row.isin}) {row.company_name}")

    if not args.apply:
        print("\nPlan only — nothing written. Re-run with --apply to execute.")
        return 0

    # Same cost gate ingest applies: entering active buys daily scoring.
    if len(promoted) > ACTIVATION_THRESHOLD and not args.yes:
        return _fail(
            f"{len(promoted)} names would be promoted to active "
            f"(> {ACTIVATION_THRESHOLD}); re-run with --yes to confirm the cost."
        )

    # Check the audience targets before the promotion writes anything, the same
    # way _apply_canonical hoists its own target check — otherwise a repo with a
    # hand-edited email_focus_tickers.csv aborts us with the universe already
    # rewritten in four repos and no way to tell from the error.
    targets = load_targets(targets_path())
    content = _rows_to_csv(plan.result)
    blocked = [r for r in distribute_email_focus(targets, content, apply=False) if not r.ok]
    if blocked:
        for result in blocked:
            print(f"  ✗ {result.target.name}: {result.message}", file=sys.stderr)
        return _fail("email-focus target check failed — nothing was written")

    universe_results = None
    if promoted:
        universe_results = _apply_canonical(promoted_result, current_rows=canonical)
        if universe_results is None:
            return _fail("universe promotion failed — email-focus file left unchanged")

    try:
        write_email_focus(email_focus_path(), plan.result)
    except OSError as exc:
        if universe_results is not None:
            print(
                f"NOTE: the {len(promoted)} promotion(s) above are already written and "
                "unpushed. Publish them with: distribute --apply",
                file=sys.stderr,
            )
        return _fail(f"could not write {email_focus_path()}: {exc}")
    print(f"✓ {email_focus_path()} updated ({len(plan.result)} rows)")

    results = distribute_email_focus(targets, content, apply=True)
    for result in results:
        marker = "✓" if result.ok else "✗"
        print(f"  {marker} {result.target.name}: {result.message}")
    if any(not r.ok for r in results):
        if universe_results is not None:
            # Pre-checked above, so reaching here means the repos changed under
            # us mid-run. The promotion is already written and unpushed; say so,
            # or the next run re-plans against a universe it thinks is unchanged.
            print(
                f"NOTE: the {len(promoted)} promotion(s) above are already written and "
                "unpushed. Publish them with: distribute --apply",
                file=sys.stderr,
            )
        return _fail("email-focus distribution did not write to every target")

    # Both publishes always run: the audience file is what this command exists
    # to deliver, so a failed universe push must not skip it.
    universe_ok = universe_results is None or _publish(
        universe_results,
        "tickers.csv",
        f"universe: activate {len(promoted)} emailed name(s) (email_scope=1)",
        no_push=args.no_push,
    )
    focus_ok = _publish(
        results, "email_focus_tickers.csv", "universe: update email_focus_tickers.csv",
        no_push=args.no_push,
    )
    return 0 if (universe_ok and focus_ok) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m research_commons.universe",
        description="Central ticker-universe management (plan-first; --apply executes).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="merge an uploaded update file")
    ingest.add_argument("file", help="path to universe_update_*.xlsx|csv or focus_update_*")
    ingest.add_argument(
        "--mode", required=True, choices=("universe", "focus"),
        help="universe = additive general update; focus = focus-list snapshot",
    )
    ingest.add_argument("--apply", action="store_true", help="execute (default: plan only)")
    ingest.add_argument(
        "--yes", action="store_true",
        help="confirm mass activations/demotions beyond the safety thresholds",
    )
    ingest.add_argument(
        "--new-status", default="watchlist", choices=("watchlist", "inactive"),
        help="scope_status for brand-new names (default watchlist: metadata only, no cost)",
    )
    ingest.add_argument(
        "--resolve", action="append", metavar="ISIN=TICKER",
        help="manual ticker resolution for collisions/suspects (repeatable)",
    )
    ingest.add_argument(
        "--allow-quarantine", action="store_true",
        help="apply the clean part even when quarantined issues remain",
    )
    ingest.add_argument(
        "--no-push", action="store_true",
        help="write + commit locally in each consumer repo but don't push "
             "(default: commit and push automatically on --apply)",
    )
    ingest.set_defaults(func=cmd_ingest)

    verify = sub.add_parser(
        "verify",
        help="read-only consistency checks: canonical-vs-DB drift, and the "
             "email_scope=1 => active delivery invariant",
    )
    verify.set_defaults(func=cmd_verify)

    sync_db = sub.add_parser("sync-db", help="upsert canonical universe into ticker_metadata")
    sync_db.set_defaults(func=cmd_sync_db)

    dist = sub.add_parser("distribute", help="regenerate consumer tickers.csv files")
    dist.add_argument("--apply", action="store_true", help="write files (default: check only)")
    dist.add_argument(
        "--no-push", action="store_true",
        help="write + commit locally but don't push (default: push automatically on --apply)",
    )
    dist.set_defaults(func=cmd_distribute)

    rename = sub.add_parser(
        "rename-ticker",
        help="rename a canonical ticker (DB PK cascade + canonical + distribution)",
    )
    rename.add_argument("old")
    rename.add_argument("new")
    rename.add_argument("--apply", action="store_true", help="execute (default: plan only)")
    rename.add_argument(
        "--no-push", action="store_true",
        help="write + commit locally but don't push (default: push automatically on --apply)",
    )
    rename.set_defaults(func=cmd_rename_ticker)

    email_focus = sub.add_parser(
        "update-email-focus",
        help="merge a PM/advisory audience upload into data/email_focus_tickers.csv",
    )
    email_focus.add_argument(
        "file", nargs="?", default=None,
        help="path to the upload file (default: most recent in universe_update_xlsx_files/)",
    )
    email_focus.add_argument("--apply", action="store_true", help="execute (default: plan only)")
    email_focus.add_argument(
        "--yes", action="store_true",
        help=f"confirm promoting more than {ACTIVATION_THRESHOLD} names to active",
    )
    email_focus.add_argument(
        "--no-push", action="store_true",
        help="write + commit locally but don't push (default: push automatically on --apply)",
    )
    email_focus.set_defaults(func=cmd_update_email_focus)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from research_commons.universe import repo_root

    try:
        repo_root()
    except RuntimeError as exc:
        return _fail(str(exc))
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
