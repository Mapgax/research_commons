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
    INACTIVE,
    STAGE_SCOPES,
    WATCHLIST,
    includes,
    normalize_scope,
    scopes_for,
    stages_for,
)


def test_watchlist_is_quant_ready_but_not_researched() -> None:
    """The point of the whole module: watchlist gets price history, nothing more."""
    assert includes("prices", WATCHLIST)
    assert includes("fundamentals", WATCHLIST)
    assert not includes("features", WATCHLIST)
    assert not includes("altdata", WATCHLIST)
    assert not includes("options", WATCHLIST)
    assert not includes("news", WATCHLIST)
    assert not includes("scoring", WATCHLIST)


def test_inactive_is_covered_but_never_scored() -> None:
    for stage in ["prices", "fundamentals", "features", "altdata", "options", "news"]:
        assert includes(stage, INACTIVE), stage
    assert not includes("scoring", INACTIVE)


def test_active_gets_every_stage() -> None:
    assert stages_for(ACTIVE) == frozenset(STAGE_SCOPES)


def test_scoring_is_the_only_active_inactive_distinction() -> None:
    """Guards the finding that motivated this module: active vs inactive earns
    exactly one bit of policy today. If a second stage ever diverges, this test
    should be updated deliberately, not by accident."""
    diverging = {
        stage
        for stage in STAGE_SCOPES
        if includes(stage, ACTIVE) != includes(stage, INACTIVE)
    }
    assert diverging == {"scoring"}


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
