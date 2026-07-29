"""Tests for distributing data/email_focus_tickers.csv — same all-or-nothing
staging discipline as tickers.csv distribution, scoped to Target.email_focus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_commons.universe import dist as dist_module
from research_commons.universe.dist import Target, distribute_email_focus


def _target(tmp_path: Path, name: str, *, email_focus: bool = True) -> Target:
    path = tmp_path / name
    path.mkdir()
    return Target(name=name, path=path, remote="example/repo", email_focus=email_focus)


def test_only_email_focus_targets_are_touched(tmp_path, monkeypatch):
    with_flag = _target(tmp_path, "with_flag", email_focus=True)
    without_flag = _target(tmp_path, "without_flag", email_focus=False)
    monkeypatch.setattr(dist_module, "check_email_focus_target", lambda *_a, **_k: "")

    results = distribute_email_focus([with_flag, without_flag], "ISIN;...\n", apply=True)

    assert [r.target.name for r in results] == ["with_flag"]
    assert (with_flag.path / "email_focus_tickers.csv").exists()
    assert not (without_flag.path / "email_focus_tickers.csv").exists()


def test_writes_exact_content_on_success(tmp_path, monkeypatch):
    target = _target(tmp_path, "companies_news")
    monkeypatch.setattr(dist_module, "check_email_focus_target", lambda *_a, **_k: "")
    content = "ISIN;company_name;scope_pm_ch;scope_pm_de;scope_advisory;email_scope\nCH0001;X;1;0;0;1\n"

    results = distribute_email_focus([target], content, apply=True)

    assert results[0].ok
    written = (target.path / "email_focus_tickers.csv").read_text(encoding="utf-8")
    assert written == content
    assert [p.name for p in target.path.iterdir()] == ["email_focus_tickers.csv"]


def test_failed_check_writes_nothing(tmp_path, monkeypatch):
    target = _target(tmp_path, "companies_news")
    monkeypatch.setattr(dist_module, "check_email_focus_target", lambda *_a, **_k: "dirty")

    results = distribute_email_focus([target], "content\n", apply=True)

    assert not results[0].ok
    assert not (target.path / "email_focus_tickers.csv").exists()


def test_plan_only_mode_writes_nothing(tmp_path, monkeypatch):
    target = _target(tmp_path, "companies_news")
    monkeypatch.setattr(dist_module, "check_email_focus_target", lambda *_a, **_k: "")

    results = distribute_email_focus([target], "content\n", apply=False)

    assert results[0].ok
    assert results[0].message == "ready"
    assert not (target.path / "email_focus_tickers.csv").exists()


def test_staging_failure_on_second_target_leaves_first_untouched(tmp_path, monkeypatch):
    first = _target(tmp_path, "first")
    second = _target(tmp_path, "second")
    (first.path / "email_focus_tickers.csv").write_text("OLD", encoding="utf-8")
    (second.path / "email_focus_tickers.csv").write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(dist_module, "check_email_focus_target", lambda *_a, **_k: "")

    import os
    import tempfile
    real_mkstemp = tempfile.mkstemp

    def failing_mkstemp(*args, **kwargs):
        if kwargs.get("dir") and Path(kwargs["dir"]).name == "second":
            raise OSError("permission denied")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", failing_mkstemp)

    results = distribute_email_focus([first, second], "NEW\n", apply=True)

    assert all(not r.ok for r in results)
    assert (first.path / "email_focus_tickers.csv").read_text(encoding="utf-8") == "OLD"
    assert (second.path / "email_focus_tickers.csv").read_text(encoding="utf-8") == "OLD"
    assert [p.name for p in first.path.iterdir()] == ["email_focus_tickers.csv"]
