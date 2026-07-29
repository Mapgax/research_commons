"""push_targets() — real git operations against local bare-repo remotes, not
mocked subprocess, so an argument mistake in the git plumbing actually fails
the test instead of passing against a fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from research_commons.universe.dist import Target, TargetResult, push_targets


def _run(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def _make_target_with_remote(tmp_path: Path, name: str) -> Target:
    """A working checkout cloned from a local bare 'origin', with an initial
    tickers.csv already committed — mirrors a real consumer repo."""
    bare = tmp_path / f"{name}_bare.git"
    bare.mkdir()
    _run("git", "init", "--bare", "-q", cwd=bare)

    checkout = tmp_path / name
    _run("git", "clone", "-q", str(bare), str(checkout), cwd=tmp_path)
    _run("git", "config", "user.email", "test@example.com", cwd=checkout)
    _run("git", "config", "user.name", "Test", cwd=checkout)
    (checkout / "tickers.csv").write_text("OLD CONTENT\n", encoding="utf-8")
    _run("git", "add", "tickers.csv", cwd=checkout)
    _run("git", "commit", "-q", "-m", "initial", cwd=checkout)
    _run("git", "push", "-q", "origin", "HEAD", cwd=checkout)

    return Target(name=name, path=checkout, remote="example/repo")


def _bare_head_content(tmp_path: Path, name: str, filename: str) -> str:
    bare = tmp_path / f"{name}_bare.git"
    result = subprocess.run(
        ["git", "show", f"HEAD:{filename}"], cwd=bare, capture_output=True, text=True, check=True,
    )
    return result.stdout


def test_successful_write_gets_committed_and_pushed(tmp_path):
    target = _make_target_with_remote(tmp_path, "first")
    (target.path / "tickers.csv").write_text("NEW CONTENT\n", encoding="utf-8")
    results = [TargetResult(target=target, ok=True, message="wrote")]

    results = push_targets(results, "tickers.csv", "test commit message")

    assert results[0].pushed is True
    assert results[0].push_message == "pushed"
    assert _bare_head_content(tmp_path, "first", "tickers.csv") == "NEW CONTENT\n"


def test_failed_write_result_is_never_pushed(tmp_path):
    target = _make_target_with_remote(tmp_path, "first")
    # No file change made — simulating a target whose write failed upstream.
    results = [TargetResult(target=target, ok=False, message="staging failed")]

    results = push_targets(results, "tickers.csv", "test commit message")

    assert results[0].pushed is False
    assert results[0].push_message == ""  # untouched — push_targets skipped it entirely
    assert _bare_head_content(tmp_path, "first", "tickers.csv") == "OLD CONTENT\n"


def test_no_actual_change_is_reported_as_pushed_not_failed(tmp_path):
    # Re-running against unchanged content ("nothing to commit") must not
    # look like a failure — it's a legitimate no-op.
    target = _make_target_with_remote(tmp_path, "first")
    # tickers.csv content is identical to what's already committed.
    results = [TargetResult(target=target, ok=True, message="wrote")]

    results = push_targets(results, "tickers.csv", "test commit message")

    assert results[0].pushed is True
    assert "nothing to commit" in results[0].push_message


def test_multiple_targets_each_get_their_own_push(tmp_path):
    first = _make_target_with_remote(tmp_path, "first")
    second = _make_target_with_remote(tmp_path, "second")
    (first.path / "tickers.csv").write_text("FIRST NEW\n", encoding="utf-8")
    (second.path / "tickers.csv").write_text("SECOND NEW\n", encoding="utf-8")
    results = [
        TargetResult(target=first, ok=True, message="wrote"),
        TargetResult(target=second, ok=True, message="wrote"),
    ]

    results = push_targets(results, "tickers.csv", "test commit message")

    assert all(r.pushed for r in results)
    assert _bare_head_content(tmp_path, "first", "tickers.csv") == "FIRST NEW\n"
    assert _bare_head_content(tmp_path, "second", "tickers.csv") == "SECOND NEW\n"


def test_push_failure_does_not_flip_ok_back_to_false(tmp_path):
    # Simulate a push failure by pointing origin at a nonexistent remote —
    # the commit still lands locally, ok stays True, only pushed/push_message
    # report the failure.
    target = _make_target_with_remote(tmp_path, "first")
    _run("git", "remote", "set-url", "origin", "/nonexistent/path.git", cwd=target.path)
    (target.path / "tickers.csv").write_text("NEW CONTENT\n", encoding="utf-8")
    results = [TargetResult(target=target, ok=True, message="wrote")]

    results = push_targets(results, "tickers.csv", "test commit message")

    assert results[0].ok is True  # write succeeded — this must stay true
    assert results[0].pushed is False
    assert "PUSH FAILED" in results[0].push_message
    # But the commit itself landed locally.
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=target.path, capture_output=True, text=True, check=True,
    )
    assert "test commit message" in log.stdout


def test_uses_the_provided_filename_not_hardcoded(tmp_path):
    target = _make_target_with_remote(tmp_path, "first")
    (target.path / "email_focus_tickers.csv").write_text("ISIN;email_scope\n", encoding="utf-8")
    _run("git", "add", "email_focus_tickers.csv", cwd=target.path)
    # Note: not committed yet — push_targets must add+commit it itself.
    results = [TargetResult(target=target, ok=True, message="wrote")]

    results = push_targets(results, "email_focus_tickers.csv", "add email focus file")

    assert results[0].pushed is True
    assert _bare_head_content(tmp_path, "first", "email_focus_tickers.csv") == "ISIN;email_scope\n"
