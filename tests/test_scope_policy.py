"""Tests for the central coverage policy.

These pin the tier semantics the whole ecosystem depends on. If one of these
changes, three production pipelines change with it — see CLAUDE.md's note that
pushing to main *is* the release.
"""

from __future__ import annotations

import pytest

from research_commons.scope_policy import (
    ACTIVE,
    ALL_SCOPES,
    DELISTED,
    INACTIVE,
    STAGE_SCOPES,
    WATCHLIST,
    includes,
    normalize_scope,
    scopes_for,
    stages_for,
)


def test_watchlist_gets_public_data_but_no_budgeted_stage() -> None:
    """Watchlist gets everything from public, unmetered sources (2026-11-14),
    nothing that spends a rate-limited or paid budget, and is not scored."""
    for stage in ["prices", "fundamentals", "features", "options", "insider"]:
        assert includes(stage, WATCHLIST), stage
    for stage in ["altdata", "news", "scoring", "scoring_daily"]:
        assert not includes(stage, WATCHLIST), stage


def test_features_superset_of_scoring() -> None:
    """Every scored name must have features; the reverse no longer holds."""
    assert scopes_for("scoring") <= scopes_for("features")


def test_inactive_is_covered_and_scored_but_not_daily() -> None:
    for stage in ["prices", "fundamentals", "features", "altdata", "insider", "options", "news", "scoring"]:
        assert includes(stage, INACTIVE), stage
    assert not includes("scoring_daily", INACTIVE)


def test_active_gets_every_stage() -> None:
    assert stages_for(ACTIVE) == frozenset(STAGE_SCOPES)


def test_scoring_cadence_is_the_only_active_inactive_distinction() -> None:
    """Guards the finding that motivated this module: active vs inactive earns
    exactly one bit of policy today — how often a name is scored, not whether.
    If a second stage ever diverges, update this deliberately, not by accident."""
    diverging = {
        stage
        for stage in STAGE_SCOPES
        if includes(stage, ACTIVE) != includes(stage, INACTIVE)
    }
    assert diverging == {"scoring_daily"}


def test_every_stage_includes_active() -> None:
    for stage, scopes in STAGE_SCOPES.items():
        assert ACTIVE in scopes, stage


def test_scopes_for_is_case_and_whitespace_insensitive() -> None:
    assert scopes_for("  PRICES ") == scopes_for("prices")


def test_scopes_for_rejects_unknown_stage() -> None:
    with pytest.raises(KeyError, match="Unknown pipeline stage"):
        scopes_for("sentiment")


def test_normalize_scope_falls_back_to_inactive_not_active() -> None:
    """A malformed row must never silently opt a ticker into scoring."""
    for raw in ["", None, "  ", "Active-ish", "unknown"]:
        assert normalize_scope(raw) in ALL_SCOPES
    assert normalize_scope("garbage") == INACTIVE
    assert normalize_scope(None) == INACTIVE
    assert normalize_scope("  ACTIVE ") == ACTIVE


def test_stage_scopes_reference_only_known_tiers() -> None:
    for stage, scopes in STAGE_SCOPES.items():
        assert scopes <= ALL_SCOPES, stage


def test_delisted_runs_no_stage() -> None:
    """A delisted name keeps its row (ISIN memory) but costs nothing anywhere."""
    assert DELISTED in ALL_SCOPES
    assert stages_for(DELISTED) == frozenset()
    assert normalize_scope("  Delisted ") == DELISTED
