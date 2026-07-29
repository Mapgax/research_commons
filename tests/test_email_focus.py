"""Tests for the PM/advisory email-focus file: read/write round-trip and the
pure merge logic (added / updated / removed-with-prompt), independent of
scope_status and of any interactive input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_commons.universe.email_focus import (
    EmailFocusRow,
    latest_update_file,
    plan_email_focus_merge,
    read_email_focus,
    read_email_focus_upload,
    write_email_focus,
)


def row(isin, name="Acme", pm_ch="0", pm_de="0", advisory="0", email="0"):
    return EmailFocusRow(
        isin=isin, company_name=name,
        scope_pm_ch=pm_ch, scope_pm_de=pm_de, scope_advisory=advisory, email_scope=email,
    )


# ── read/write round-trip ───────────────────────────────────────────────

def test_missing_canonical_file_reads_as_empty(tmp_path):
    assert read_email_focus(tmp_path / "nope.csv") == []


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "email_focus_tickers.csv"
    rows = [
        row("CH0001", "Swiss Life", pm_ch="1", pm_de="0", advisory="0", email="1"),
        row("AT0002", "BAWAG", pm_ch="0", pm_de="1", advisory="1", email="0"),
    ]
    write_email_focus(path, rows)

    loaded = read_email_focus(path)
    assert {r.isin for r in loaded} == {"CH0001", "AT0002"}
    by_isin = {r.isin: r for r in loaded}
    assert by_isin["CH0001"].scope_pm_ch == "1"
    assert by_isin["CH0001"].email_scope == "1"
    assert by_isin["AT0002"].scope_advisory == "1"


def test_written_file_is_semicolon_delimited_with_expected_header(tmp_path):
    path = tmp_path / "email_focus_tickers.csv"
    write_email_focus(path, [row("CH0001", email="1")])
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "ISIN;company_name;scope_pm_ch;scope_pm_de;scope_advisory;email_scope"
    assert ";" in text
    assert "," not in text.splitlines()[1]  # no stray commas in the data row


def test_read_upload_csv_same_six_columns(tmp_path):
    path = tmp_path / "upload.csv"
    path.write_text(
        "ISIN;company_name;scope_pm_ch;scope_pm_de;scope_advisory;email_scope\n"
        "CH0001;Swiss Life;1;0;0;1\n",
        encoding="utf-8",
    )
    rows = read_email_focus_upload(path)
    assert len(rows) == 1
    assert rows[0].isin == "CH0001"
    assert rows[0].email_scope == "1"


def test_read_upload_rejects_unsupported_extension(tmp_path):
    path = tmp_path / "upload.pdf"
    path.write_text("nonsense", encoding="utf-8")
    with pytest.raises(ValueError):
        read_email_focus_upload(path)


def test_latest_update_file_picks_most_recently_modified(tmp_path):
    import os
    import time

    older = tmp_path / "email_focus_202607.csv"
    newer = tmp_path / "email_focus_202608.csv"
    older.write_text("ISIN;company_name;scope_pm_ch;scope_pm_de;scope_advisory;email_scope\n", encoding="utf-8")
    time.sleep(0.01)
    newer.write_text("ISIN;company_name;scope_pm_ch;scope_pm_de;scope_advisory;email_scope\n", encoding="utf-8")

    assert latest_update_file(tmp_path) == newer


def test_latest_update_file_none_when_directory_empty(tmp_path):
    assert latest_update_file(tmp_path) is None


# ── merge logic ─────────────────────────────────────────────────────────

def test_new_isin_in_upload_is_added_without_prompting(tmp_path):
    current = []
    upload = [row("CH0001", "Swiss Life", pm_ch="1", email="1")]

    def refuse_to_be_called(_row):
        raise AssertionError("resolve_removed must not be called for additions")

    plan = plan_email_focus_merge(current, upload, resolve_removed=refuse_to_be_called)
    assert len(plan.added) == 1
    assert plan.added[0].isin == "CH0001"
    assert plan.updated == []
    assert plan.kept == []
    assert plan.zeroed == []
    assert [r.isin for r in plan.result] == ["CH0001"]


def test_isin_in_both_is_overwritten_without_prompting(tmp_path):
    current = [row("CH0001", "Swiss Life", pm_ch="0", email="0")]
    upload = [row("CH0001", "Swiss Life", pm_ch="1", email="1")]

    def refuse_to_be_called(_row):
        raise AssertionError("resolve_removed must not be called for updates")

    plan = plan_email_focus_merge(current, upload, resolve_removed=refuse_to_be_called)
    assert plan.added == []
    assert len(plan.updated) == 1
    old, new = plan.updated[0]
    assert old.email_scope == "0"
    assert new.email_scope == "1"
    assert [r.isin for r in plan.result] == ["CH0001"]
    assert plan.result[0].email_scope == "1"


def test_isin_missing_from_upload_prompts_and_keep_preserves_pm_email(tmp_path):
    current = [row("CH0001", "Swiss Life", pm_ch="1", pm_de="1", advisory="1", email="1")]
    upload = []  # CH0001 fell out of this upload

    plan = plan_email_focus_merge(current, upload, resolve_removed=lambda r: True)  # "keep"

    assert plan.kept[0].isin == "CH0001"
    assert plan.kept[0].scope_pm_ch == "1"
    assert plan.kept[0].scope_pm_de == "1"
    assert plan.kept[0].email_scope == "1"
    # advisory ALWAYS resets to 0 on removal, regardless of the keep/zero answer
    assert plan.kept[0].scope_advisory == "0"
    assert plan.zeroed == []


def test_isin_missing_from_upload_prompts_and_zero_clears_everything(tmp_path):
    current = [row("CH0001", "Swiss Life", pm_ch="1", pm_de="1", advisory="1", email="1")]
    upload = []

    plan = plan_email_focus_merge(current, upload, resolve_removed=lambda r: False)  # "zero"

    assert plan.kept == []
    assert plan.zeroed[0].isin == "CH0001"
    assert plan.zeroed[0].scope_pm_ch == "0"
    assert plan.zeroed[0].scope_pm_de == "0"
    assert plan.zeroed[0].scope_advisory == "0"
    assert plan.zeroed[0].email_scope == "0"


def test_advisory_always_zeroed_on_removal_even_when_kept(tmp_path):
    # Direct check of the specific rule: "missing implies false for advisory
    # specifically" — independent of whatever the keep/zero answer was.
    current = [row("CH0001", advisory="1")]
    plan_keep = plan_email_focus_merge(current, [], resolve_removed=lambda r: True)
    plan_zero = plan_email_focus_merge(current, [], resolve_removed=lambda r: False)
    assert plan_keep.result[0].scope_advisory == "0"
    assert plan_zero.result[0].scope_advisory == "0"


def test_mixed_batch_adds_updates_and_removals_together(tmp_path):
    current = [
        row("CH0001", "Swiss Life", pm_ch="1", email="1"),   # stays, updated
        row("AT0002", "BAWAG", pm_ch="0", email="0"),         # drops out of upload
    ]
    upload = [
        row("CH0001", "Swiss Life", pm_ch="1", email="0"),    # email_scope flips
        row("DE0003", "New Co", pm_ch="1", email="1"),        # brand new
    ]

    resolved_isins = []

    def resolver(r):
        resolved_isins.append(r.isin)
        return True  # keep

    plan = plan_email_focus_merge(current, upload, resolve_removed=resolver)

    assert resolved_isins == ["AT0002"]
    assert {r.isin for r in plan.added} == {"DE0003"}
    assert len(plan.updated) == 1
    assert plan.updated[0][1].email_scope == "0"
    assert {r.isin for r in plan.kept} == {"AT0002"}
    assert {r.isin for r in plan.result} == {"CH0001", "DE0003", "AT0002"}


def test_isin_absent_from_current_and_upload_never_appears(tmp_path):
    # Sanity: an ISIN that was never tracked and isn't in this upload simply
    # doesn't show up anywhere in the plan — "absent = 0" needs no row at all.
    current = [row("CH0001")]
    upload = [row("CH0001")]
    plan = plan_email_focus_merge(current, upload, resolve_removed=lambda r: True)
    assert all(r.isin == "CH0001" for r in plan.result)
    assert len(plan.result) == 1
