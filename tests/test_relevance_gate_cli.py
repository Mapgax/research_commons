from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

from research_commons.db_news.relevance import WeeklyDrift

_SPEC = importlib.util.spec_from_file_location(
    "relevance_gate_cli", Path(__file__).resolve().parents[1] / "scripts" / "relevance_gate.py",
)
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cli
_SPEC.loader.exec_module(cli)

MONDAY = date(2026, 9, 7)
TODAY = date(2026, 10, 14)  # a Wednesday: the week of 2026-10-12 is partial


def _week(n: int, *, judged: int = 950, attempted: int = 960, failed: int = 10,
          median: float = 0.4, articles_pass: int = 300,
          judges: tuple[str, ...] = ("haiku-v1/claude-haiku-4-5",)) -> WeeklyDrift:
    return WeeklyDrift(
        week=MONDAY + timedelta(weeks=n), n_links=1000, n_judged=judged,
        n_attempted=attempted, n_failed=failed,
        p_quantiles=(0.05, 0.1, median, 0.8, 0.95) if judged else None,
        n_near_half=50, n_judged_pass=400, n_articles=600, n_articles_pass=articles_pass,
        judges=judges,
    )


def _flags(weeks: list[WeeklyDrift]) -> dict[date, list[str]]:
    return cli.drift_flags(weeks, today=TODAY, failure_warn_rate=0.10)


def test_steady_weeks_raise_no_flags() -> None:
    assert _flags([_week(i) for i in range(6)]) == {}


def test_failures_and_low_coverage_are_flagged() -> None:
    weeks = [_week(0), _week(1), _week(2, failed=200, judged=700)]
    [reasons] = _flags(weeks).values()
    assert any(r.startswith("failures") for r in reasons)
    assert any(r.startswith("judged 70%") for r in reasons)


def test_weeks_before_go_live_are_not_judged_on_coverage() -> None:
    dark = [_week(i, judged=0, attempted=0, failed=0) for i in range(3)]
    assert _flags(dark) == {}
    # once live, a later week with no attempts at all is an outage
    weeks = dark + [_week(3), _week(4, judged=0, attempted=0, failed=0)]
    assert _flags(weeks) == {weeks[-1].week: ["judged 0% < 90%"]}


def test_the_partial_current_week_is_not_judged_on_coverage() -> None:
    weeks = [_week(i) for i in range(5)] + [_week(5, judged=100, attempted=100, failed=0)]
    assert weeks[-1].week == date(2026, 10, 12)
    flags = _flags(weeks)
    assert not any(r.startswith("judged") for r in flags.get(weeks[-1].week, []))


def test_shifts_against_earlier_weeks_are_flagged() -> None:
    weeks = [_week(0), _week(1), _week(2), _week(3, median=0.7, articles_pass=420)]
    reasons = _flags(weeks)[weeks[-1].week]
    assert "pass share 70% vs 50%" in reasons
    assert "median p 0.70 vs 0.40" in reasons
    # no baseline yet -> no shift flags in the first weeks
    assert _flags([_week(0, median=0.9), _week(1)]) == {}


def test_a_new_judge_version_is_flagged_once() -> None:
    v2 = ("haiku-v1/claude-haiku-4-5", "haiku-v2/claude-haiku-4-5")
    weeks = [_week(0), _week(1, judges=v2), _week(2, judges=v2)]
    flags = _flags(weeks)
    assert flags == {weeks[1].week: ["new judge haiku-v2/claude-haiku-4-5"]}


def test_render_drift_marks_partial_week_and_lists_flags() -> None:
    weeks = [_week(4), _week(5, judged=0, attempted=0, failed=0)]
    text = cli.render_drift(weeks, {weeks[0].week: ["failures 20% > 10%"]},
                            threshold=0.5, today=TODAY)
    assert "(partial)" in text.splitlines()[-4]
    assert "2026-10-05: failures 20% > 10%" in text
