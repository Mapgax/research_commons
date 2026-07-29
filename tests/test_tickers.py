"""Tests for the consolidated ticker/Yahoo/currency derivation.

The golden-file test pins the consolidated derivation to MSARN's historical
behavior (``tests/golden_yahoo_symbols.csv`` was generated from MSARN's live
``TICKER_TO_YAHOO`` at consolidation time). MSARN's derivation wins because
it is the one continuously validated against Yahoo by the price collectors.
"""

from __future__ import annotations

import csv
from pathlib import Path

from research_commons.tickers import (
    derive_yahoo_symbol,
    normalize_ticker,
    parse_bloomberg_to_currency,
    parse_bloomberg_to_yahoo_symbol,
)

GOLDEN = Path(__file__).parent / "golden_yahoo_symbols.csv"


def test_normalize_ticker() -> None:
    assert normalize_ticker("AAPL US Equity") == "AAPL"
    assert normalize_ticker("  novn sw equity ") == "NOVN"
    assert normalize_ticker("") == ""
    assert normalize_ticker(None) == ""  # type: ignore[arg-type]


def test_parse_bloomberg_to_yahoo_symbol_examples() -> None:
    assert parse_bloomberg_to_yahoo_symbol("IXN US Equity") == "IXN"
    assert parse_bloomberg_to_yahoo_symbol("NOVN SW Equity") == "NOVN.SW"
    assert parse_bloomberg_to_yahoo_symbol("MC FP Equity") == "MC.PA"
    assert parse_bloomberg_to_yahoo_symbol("BRK/B US Equity") == "BRK-B"
    assert parse_bloomberg_to_yahoo_symbol("UU/ LN Equity") == "UU.L"
    assert parse_bloomberg_to_yahoo_symbol("700 HK Equity") == "0700.HK"
    assert parse_bloomberg_to_yahoo_symbol("NEM GY Equity") == "NEM.DE"
    assert parse_bloomberg_to_yahoo_symbol("") == ""


def test_derive_yahoo_symbol_priority() -> None:
    # Manual override wins.
    assert derive_yahoo_symbol("CFR", "CFR SW Equity") == "CFR.SW"
    assert derive_yahoo_symbol("NOVOB", "NOVOB DC Equity") == "NOVO-B.CO"
    # Dotted canonical passes through (dot-suffix collision resolutions).
    assert derive_yahoo_symbol("NEM.DE", "NEM GY Equity") == "NEM.DE"
    # Bloomberg derivation as fallback.
    assert derive_yahoo_symbol("ABBN", "ABBN SW Equity") == "ABBN.SW"
    # Bare canonical when nothing else is known.
    assert derive_yahoo_symbol("AAPL", "") == "AAPL"


def test_parse_bloomberg_to_currency() -> None:
    assert parse_bloomberg_to_currency("SAP GR Equity") == "EUR"
    assert parse_bloomberg_to_currency("NOVN SW Equity") == "CHF"
    assert parse_bloomberg_to_currency("AZN LN Equity") == "GBP"
    assert parse_bloomberg_to_currency("NOVOB DC Equity") == "DKK"
    assert parse_bloomberg_to_currency("AAPL US Equity") == "USD"
    assert parse_bloomberg_to_currency("") == "USD"


def test_golden_yahoo_symbols_unchanged() -> None:
    """Every pre-consolidation name must derive exactly its historical symbol."""
    with GOLDEN.open("r", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert len(rows) >= 141
    mismatches = {}
    for row in rows:
        derived = derive_yahoo_symbol(row["ticker"], row["bloomberg_ticker"])
        if derived != row["yahoo_symbol"]:
            mismatches[row["ticker"]] = (row["yahoo_symbol"], derived)
    assert not mismatches, f"derivation drifted from MSARN golden: {mismatches}"
