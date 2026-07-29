"""Distribution: regenerate each consumer repo's tickers.csv from canonical.

Targets are declared in ``universe_targets.toml`` at the repo root, with the
expected git remote per target. Before writing, each target must:

  * exist and be a git worktree,
  * have an ``origin`` remote matching the expected one (guards against a
    stale clone after a repo moves — it has happened), and
  * have a clean ``tickers.csv`` in git (no uncommitted local edits to clobber).

Writes are temp-file + atomic rename. Nothing is committed automatically; the
tool prints per-repo status and the exact commands to run.
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from research_commons.universe.io import (
    UniverseRow,
    commit_staged,
    discard_staged,
    render_consumer_csv,
    stage_consumer_csv,
)


@dataclass
class Target:
    name: str
    path: Path
    remote: str


@dataclass
class TargetResult:
    target: Target
    ok: bool
    message: str


def load_targets(toml_path: Path) -> list[Target]:
    if not toml_path.exists():
        raise FileNotFoundError(
            f"Distribution targets file not found: {toml_path}\n"
            "Create universe_targets.toml with [[target]] entries (name, path, remote)."
        )
    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)
    targets = []
    for entry in data.get("target", []):
        targets.append(
            Target(
                name=str(entry["name"]),
                path=Path(str(entry["path"])).expanduser(),
                remote=str(entry["remote"]),
            )
        )
    if not targets:
        raise ValueError(f"No [[target]] entries found in {toml_path}")
    return targets


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def check_target(target: Target, current_render: str | None = None) -> str:
    """Return an error message, or empty string when the target is writable.

    ``current_render`` is the consumer-CSV rendering of the *current* canonical
    state: a dirty tickers.csv whose content matches it byte-for-byte was
    written by this tool and is merely awaiting its commit — overwriting it is
    safe. Anything else dirty is treated as a hand edit and blocks.
    """
    if not target.path.is_dir():
        return f"path does not exist: {target.path}"
    remote_proc = _git(target.path, "remote", "get-url", "origin")
    if remote_proc.returncode != 0:
        return f"not a git repository (or no origin remote): {target.path}"
    actual_remote = remote_proc.stdout.strip()
    if target.remote.lower() not in actual_remote.lower():
        return (
            f"origin remote mismatch: expected …{target.remote}, found {actual_remote} "
            "(stale clone? fix universe_targets.toml or the checkout)"
        )
    status_proc = _git(target.path, "status", "--porcelain", "--", "tickers.csv")
    if status_proc.returncode != 0:
        return f"git status failed: {status_proc.stderr.strip()}"
    if status_proc.stdout.strip():
        if current_render is not None:
            try:
                on_disk = (target.path / "tickers.csv").read_text(encoding="utf-8")
            except OSError as exc:
                return f"cannot read tickers.csv: {exc}"
            if on_disk == current_render:
                return ""  # tool-authored, awaiting commit — safe to overwrite
        return (
            "tickers.csv has uncommitted local changes — commit or discard them first "
            "so distribution cannot clobber hand edits"
        )
    return ""


def distribute(
    targets: list[Target],
    rows: list[UniverseRow],
    *,
    apply: bool,
    current_rows: list[UniverseRow] | None = None,
) -> list[TargetResult]:
    """Check every target; write tickers.csv into each when ``apply`` is set.

    All targets are checked before any write, and the writes themselves run as
    a two-phase commit (stage every file, then rename every file), so the
    consumer repos cannot end up holding different universe versions — the
    divergence this tool exists to prevent. ``current_rows`` is the pre-merge
    canonical state used to recognize tool-authored uncommitted files (see
    :func:`check_target`).
    """
    current_render = render_consumer_csv(current_rows) if current_rows is not None else None
    results: list[TargetResult] = []
    errors = False
    for target in targets:
        problem = check_target(target, current_render)
        if problem:
            results.append(TargetResult(target=target, ok=False, message=problem))
            errors = True
        else:
            results.append(TargetResult(target=target, ok=True, message="ready"))

    if errors or not apply:
        return results

    # Phase 1 — stage every target; any failure here writes nothing anywhere.
    staged: list[tuple[TargetResult, Path, Path]] = []
    try:
        for result in results:
            out_path = result.target.path / "tickers.csv"
            staged.append((result, stage_consumer_csv(out_path, rows), out_path))
    except OSError as exc:
        for _, tmp_path, _ in staged:
            discard_staged(tmp_path)
        for result in results:
            result.ok = False
            result.message = f"staging failed, nothing written: {exc}"
        return results

    # Phase 2 — commit. os.replace is atomic; a failure mid-loop is not
    # expected, but leftover temp files must never be left in a git worktree.
    for index, (result, tmp_path, out_path) in enumerate(staged):
        try:
            commit_staged(tmp_path, out_path)
        except OSError as exc:
            for _, pending_tmp, _ in staged[index:]:
                discard_staged(pending_tmp)
            for pending_result, _, _ in staged[index:]:
                pending_result.ok = False
                pending_result.message = f"commit failed: {exc}"
            return results
        result.message = f"wrote {out_path}"
    return results


def commit_instructions(results: list[TargetResult], summary: str) -> str:
    lines = ["To publish the update, commit and push each repo:"]
    for result in results:
        if not result.ok:
            continue
        lines.append("")
        lines.append(f"# {result.target.name}")
        lines.append(
            f"git -C {result.target.path} add tickers.csv && "
            f"git -C {result.target.path} commit -m {summary!r} && "
            f"git -C {result.target.path} push"
        )
    return "\n".join(lines)
