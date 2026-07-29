"""Tests for the universe merge + issue-detection engine.

The fixtures replicate the real issue cases found in the first production
update file (tickers_20260728.xlsx): the NEM base-ticker collision
(Nemetschek vs Newmont), the Inficon superseded-ISIN pair, and the Iberdrola
rights lines.
"""

from __future__ import annotations

from datetime import date

from research_commons.universe.io import IgnoreEntry, UniverseRow, UpdateRow
from research_commons.universe.merge import (
    COLLISION,
    CURRENCY_MISMATCH,
    DUPLICATE_IN_FILE,
    INVALID_ISIN,
    MISSING_FIELDS,
    SUPERSEDED_ISIN,
    SUSPECT_NON_EQUITY,
    TICKER_STALE,
    UNKNOWN_ISIN,
    isin_is_valid,
    looks_like_non_equity,
    plan_focus_merge,
    plan_universe_merge,
)

TODAY = date(2026, 7, 29)


def canon(
    ticker: str,
    isin: str,
    bbg: str,
    status: str = "inactive",
    **kwargs: object,
) -> UniverseRow:
    return UniverseRow(
        company_name=kwargs.pop("company_name", f"{ticker} Corp"),  # type: ignore[arg-type]
        isin=isin,
        bloomberg_ticker=bbg,
        ticker=ticker,
        scope_status=status,
        currency=str(kwargs.pop("currency", "USD")),
        yahoo_symbol=str(kwargs.pop("yahoo_symbol", ticker)),
        **kwargs,  # type: ignore[arg-type]
    )


def upd(name: str, isin: str, bbg: str, currency: str = "", line: int = 2) -> UpdateRow:
    return UpdateRow(
        company_name=name,
        isin=isin,
        bloomberg_ticker=bbg,
        currency=currency,
        source_line=line,
    )


# --- ISIN validation ---------------------------------------------------------


def test_isin_checksum() -> None:
    for good in ("US0378331005", "CH0012032048", "DE0006452907", "CA13646K1084"):
        assert isin_is_valid(good)
    # Input is normalized (strip + uppercase) before validation.
    assert isin_is_valid("us0378331005 ")
    for bad in ("US0378331006", "US037833100", "", "1230378331005"):
        assert not isin_is_valid(bad)


# --- structural non-equity detection ----------------------------------------


def test_rights_lines_detected() -> None:
    for name in ("IBERDROLA SA - RTS", "IBERDROLA SA-RTS", "IBERDROLA SA -RTS"):
        assert looks_like_non_equity(upd(name, "ES06445809T5", "IBE/D SM Equity"))


def test_slash_class_detection() -> None:
    # IBE/D: "D" is not a known share class -> suspect even with a clean name.
    assert looks_like_non_equity(upd("Iberdrola SA", "ES06445809T5", "IBE/D SM Equity"))
    # BRK/B is a legitimate share class.
    assert not looks_like_non_equity(upd("Berkshire", "US0846707026", "BRK/B US Equity"))
    # Trailing slash shorthand (UU/ LN) is fine.
    assert not looks_like_non_equity(upd("United Utilities", "GB00B39J2M42", "UU/ LN Equity"))
    # -REG name suffixes are fine.
    assert not looks_like_non_equity(
        upd("INFICON HOLDING AG-REG", "CH1431598916", "IFCN SW Equity")
    )


# --- universe merge ----------------------------------------------------------


def test_plain_add_gets_watchlist_and_enrichment() -> None:
    canonical = [canon("AAPL", "US0378331005", "AAPL US Equity")]
    plan = plan_universe_merge(
        canonical,
        [upd("Roche Holding AG", "CH0012032048", "ROG SW Equity", "CHF")],
        [],
        today=TODAY,
    )
    assert not plan.quarantined
    assert len(plan.adds) == 1
    added = plan.adds[0]
    assert added.ticker == "ROG"
    assert added.yahoo_symbol == "ROG.SW"
    assert added.currency == "CHF"
    assert added.scope_status == "watchlist"
    assert added.added_date == "2026-07-29"
    assert len(plan.result) == 2


def test_existing_isin_refreshes_not_duplicates() -> None:
    canonical = [canon("AAPL", "US0378331005", "AAPL US Equity", company_name="Apple")]
    plan = plan_universe_merge(
        canonical,
        [upd("Apple Inc", "US0378331005", "AAPL US Equity")],
        [],
        today=TODAY,
    )
    assert not plan.adds
    assert len(plan.refreshes) == 1
    assert plan.refreshes[0].changes["company_name"] == ("Apple", "Apple Inc")
    assert len(plan.result) == 1


def test_nem_collision_quarantined_with_dot_suffix_suggestion() -> None:
    canonical = [canon("NEM", "US6516391066", "NEM UN Equity", company_name="Newmont Corp")]
    plan = plan_universe_merge(
        canonical,
        [upd("Nemetschek SE", "DE0006452907", "NEM GY Equity", "EUR")],
        [],
        today=TODAY,
    )
    assert len(plan.quarantined) == 1
    issue = plan.quarantined[0]
    assert issue.category == COLLISION
    assert "NEM.DE" in issue.suggestion
    # Newmont untouched, Nemetschek not added.
    assert len(plan.result) == 1


def test_nem_collision_resolved_via_flag() -> None:
    canonical = [canon("NEM", "US6516391066", "NEM UN Equity", company_name="Newmont Corp")]
    plan = plan_universe_merge(
        canonical,
        [upd("Nemetschek SE", "DE0006452907", "NEM GY Equity", "EUR")],
        [],
        resolutions={"DE0006452907": "NEM.DE"},
        today=TODAY,
    )
    assert not plan.quarantined
    assert plan.adds[0].ticker == "NEM.DE"
    assert plan.adds[0].yahoo_symbol == "NEM.DE"


def test_inficon_superseded_isin_case() -> None:
    canonical = [
        canon(
            "IFCN",
            "CH1431598916",
            "IFCN SW Equity",
            company_name="INFICON HOLDING AG-REG",
        )
    ]
    updates = [
        upd("Inficon", "CH0011029946", "IFCN SW Equity", "CHF", line=2),
        upd("INFICON HOLDING AG-REG", "CH1431598916", "IFCN SW Equity", "CHF", line=3),
    ]
    plan = plan_universe_merge(canonical, updates, [], today=TODAY)
    superseded = [i for i in plan.quarantined if i.category == SUPERSEDED_ISIN]
    assert len(superseded) == 1
    assert superseded[0].isin == "CH0011029946"
    assert "ignore" in superseded[0].suggestion.lower()
    assert not plan.adds


def test_superseded_isin_recorded_on_row_suppresses() -> None:
    canonical = [
        canon(
            "IFCN",
            "CH1431598916",
            "IFCN SW Equity",
            superseded_isins="CH0011029946",
        )
    ]
    plan = plan_universe_merge(
        canonical,
        [upd("Inficon", "CH0011029946", "IFCN SW Equity")],
        [],
        today=TODAY,
    )
    assert not plan.quarantined
    assert len(plan.suppressed) == 1


def test_ignore_list_suppresses_rights_lines() -> None:
    ignore = [IgnoreEntry(isin="ES06445809T5", bloomberg_ticker="", reason="rights line")]
    plan = plan_universe_merge(
        [],
        [upd("IBERDROLA SA - RTS", "ES06445809T5", "IBE/D SM Equity")],
        ignore,
        today=TODAY,
    )
    assert not plan.quarantined
    assert plan.suppressed and plan.suppressed[0][1] == "rights line"


def test_rights_line_quarantined_without_ignore_entry() -> None:
    plan = plan_universe_merge(
        [],
        [upd("IBERDROLA SA - RTS", "ES06445809T5", "IBE/D SM Equity")],
        [],
        today=TODAY,
    )
    assert [i.category for i in plan.quarantined] == [SUSPECT_NON_EQUITY]


def test_invalid_isin_and_missing_fields() -> None:
    plan = plan_universe_merge(
        [],
        [
            upd("Broken Co", "XX0000000001", "BRK1 US Equity"),
            upd("No Bloomberg", "US0378331005", ""),
        ],
        [],
        today=TODAY,
    )
    categories = sorted(i.category for i in plan.quarantined)
    assert categories == [INVALID_ISIN, MISSING_FIELDS]


def test_conflicting_duplicate_in_file() -> None:
    plan = plan_universe_merge(
        [],
        [
            upd("Company A", "US0378331005", "AAPL US Equity", line=2),
            upd("Company B", "US0378331005", "AAPL2 US Equity", line=3),
        ],
        [],
        today=TODAY,
    )
    assert any(i.category == DUPLICATE_IN_FILE for i in plan.issues)


def test_identical_duplicate_collapses_silently() -> None:
    plan = plan_universe_merge(
        [],
        [
            upd("Apple Inc", "US0378331005", "AAPL US Equity", line=2),
            upd("Apple Inc", "US0378331005", "AAPL US Equity", line=3),
        ],
        [],
        today=TODAY,
    )
    assert not plan.issues
    assert len(plan.adds) == 1


def test_add_currency_derived_from_exchange_beats_dencurrency() -> None:
    plan = plan_universe_merge(
        [],
        [upd("Novartis AG", "CH0012005267", "NOVN SW Equity", currency="EUR")],
        [],
        today=TODAY,
    )
    assert len(plan.adds) == 1
    # DenCurrency (EUR) is a denomination currency; the SW exchange wins.
    assert plan.adds[0].currency == "CHF"
    assert [i.category for i in plan.warnings] == [CURRENCY_MISMATCH]


def test_add_currency_uses_file_value_when_exchange_unknown() -> None:
    plan = plan_universe_merge(
        [],
        [upd("Mystery Corp", "US0378331005", "MYST ZZ Equity", currency="EUR")],
        [],
        today=TODAY,
    )
    assert plan.adds[0].currency == "EUR"
    assert not plan.warnings


def test_refresh_never_touches_currency() -> None:
    canonical = [
        canon("RIO", "GB0007188757", "RIO LN Equity", currency="GBP", status="active")
    ]
    plan = plan_universe_merge(
        canonical,
        [upd("Rio Tinto PLC", "GB0007188757", "RIO LN Equity", currency="EUR")],
        [],
        today=TODAY,
    )
    assert plan.result[0].currency == "GBP"
    assert [i.category for i in plan.warnings] == [CURRENCY_MISMATCH]
    assert all("currency" not in r.changes for r in plan.refreshes)


def test_replaced_listing_warns_that_a_rename_is_needed() -> None:
    canonical = [
        canon("SOLB", "BE0003604155", "SOLB BB Equity", company_name="Solvay SA")
    ]
    plan = plan_universe_merge(
        canonical,
        [upd("LOTUS BAKERIES", "BE0003604155", "LOTB BB Equity")],
        [],
        today=TODAY,
    )
    # Warning only: the rename cascades a DB primary key and stays manual.
    assert not plan.quarantined
    assert plan.result[0].ticker == "SOLB"
    stale = [i for i in plan.warnings if i.category == TICKER_STALE]
    assert len(stale) == 1
    assert "rename-ticker SOLB LOTB" in stale[0].suggestion


def test_dot_suffix_resolutions_do_not_warn_forever() -> None:
    canonical = [
        canon("NEM.DE", "DE0006452907", "NEM GY Equity", company_name="Nemetschek SE")
    ]
    plan = plan_universe_merge(
        canonical,
        [upd("Nemetschek SE AG", "DE0006452907", "NEM GY Equity")],
        [],
        today=TODAY,
    )
    assert not [i for i in plan.issues if i.category == TICKER_STALE]


def test_universe_merge_never_demotes() -> None:
    canonical = [canon("AAPL", "US0378331005", "AAPL US Equity", status="active")]
    plan = plan_universe_merge(
        canonical,
        [upd("Apple Inc", "US0378331005", "AAPL US Equity")],
        [],
        today=TODAY,
    )
    assert plan.result[0].scope_status == "active"
    assert not plan.demotions


# --- focus merge -------------------------------------------------------------


def test_focus_promotes_and_demotes() -> None:
    canonical = [
        canon("AAA", "US0378331005", "AAA US Equity", status="active"),
        canon("BBB", "US5949181045", "BBB US Equity", status="inactive"),
        canon("CCC", "US67066G1040", "CCC US Equity", status="watchlist"),
    ]
    plan = plan_focus_merge(
        canonical,
        [
            upd("B Corp", "US5949181045", "BBB US Equity"),
            upd("C Corp", "US67066G1040", "CCC US Equity"),
        ],
        [],
    )
    assert {r.ticker for r in plan.promotions} == {"BBB", "CCC"}
    assert [r.ticker for r in plan.demotions] == ["AAA"]
    statuses = {r.ticker: r.scope_status for r in plan.result}
    assert statuses == {"AAA": "inactive", "BBB": "active", "CCC": "active"}


def test_focus_unknown_isin_quarantined_not_added() -> None:
    canonical = [canon("AAA", "US0378331005", "AAA US Equity", status="active")]
    plan = plan_focus_merge(
        canonical,
        [
            upd("A Corp", "US0378331005", "AAA US Equity"),
            upd("Stranger", "CH0012032048", "ROG SW Equity"),
        ],
        [],
    )
    assert [i.category for i in plan.quarantined] == [UNKNOWN_ISIN]
    assert len(plan.result) == 1
    assert not plan.demotions  # AAA is in the file, stays active


def test_focus_never_touches_universe_membership() -> None:
    canonical = [canon("AAA", "US0378331005", "AAA US Equity", status="inactive")]
    plan = plan_focus_merge(canonical, [upd("A", "US0378331005", "AAA US Equity")], [])
    assert len(plan.result) == len(canonical)
    assert plan.result[0].scope_status == "active"
