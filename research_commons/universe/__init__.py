"""Central ticker-universe management.

One place to upload a ticker update for the whole ecosystem:

    python3 -m research_commons.universe ingest <file> --mode universe|focus

The tool merges update files (Bloomberg xlsx/csv exports) into the canonical
universe file (``data/ticker_universe.csv`` at the repo root), mirrors the
result into ``market.ticker_metadata`` (single writer), regenerates each
consumer repo's ``tickers.csv``, archives inputs, and reports every issue that
needs manual resolution.

Runs are plan-only by default; ``--apply`` executes.

This package must run from a git checkout of research_commons (data files and
reports live at the repo root, outside the installed package).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    """Return the research_commons checkout root, or raise if installed.

    The canonical universe file, ignore list, archives, and reports are
    repo-root artifacts (git-versioned). An installed copy of the package has
    none of them, so the CLI refuses to operate there.
    """
    if not (_REPO_ROOT / "pyproject.toml").exists() or not (_REPO_ROOT / "migrations").exists():
        raise RuntimeError(
            "research_commons.universe must run from a git checkout of research_commons "
            "(data/ and reports/ live at the repo root); an installed copy will not work."
        )
    return _REPO_ROOT


def canonical_path() -> Path:
    return repo_root() / "data" / "ticker_universe.csv"


def ignore_path() -> Path:
    return repo_root() / "data" / "universe_ignore.csv"


def targets_path() -> Path:
    return repo_root() / "universe_targets.toml"


def archive_dir() -> Path:
    return repo_root() / "data" / "archive" / "updates"


def reports_dir() -> Path:
    return repo_root() / "reports" / "universe"


def email_focus_path() -> Path:
    """The PM/advisory email-audience file — independent of scope_status.

    See ``research_commons/universe/email_focus.py`` for the merge logic
    and ``update-email-focus`` in the CLI for the command that writes it.
    """
    return repo_root() / "data" / "email_focus_tickers.csv"


def email_focus_update_dir() -> Path:
    return repo_root() / "universe_update_xlsx_files"
