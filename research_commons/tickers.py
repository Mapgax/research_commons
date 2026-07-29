"""Canonical ticker registry.

This module is the *single source of truth* for which logical ticker symbols
exist in the ecosystem. The registry state lives in DB1 (``ticker_metadata``
table), maintained by ``research_commons.universe`` (the central ingest tool).
Project-specific ``tickers.csv`` files are generated from the canonical
universe file by the same tool; consumer runtime stays CSV-authoritative until
a later migration phase.

Helpers here normalize Yahoo Finance / Bloomberg / ISIN identifiers and map a
ticker to its local trading currency (ISO 4217). The mapping tables below are
the consolidated union of the previously drifted per-project copies (MSARN
``config.py`` and Companies_News ``ticker_universe.py``); MSARN's derivation
wins on conflicts because it is the one continuously validated against Yahoo
by the price collectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class TickerInfo:
    ticker: str          # logical symbol used internally, e.g. "NOVN"
    company_name: str
    isin: str | None
    bloomberg_ticker: str | None
    yahoo_symbol: str    # e.g. "NOVN.SW"
    sector_name: str | None
    currency: str        # ISO 4217, e.g. "CHF"
    scope_status: str = "inactive"  # "active" | "inactive" | "watchlist"
    # Default is "inactive" (not "active") so a TickerInfo constructed without
    # an explicit scope_status never silently opts a ticker into production pipelines.


# Bloomberg exchange code -> Yahoo Finance symbol suffix.
BLOOMBERG_EXCHANGE_TO_YAHOO_SUFFIX: dict[str, str] = {
    "US": "",
    "UW": "",
    "UN": "",
    "LN": ".L",
    "FP": ".PA",
    "GR": ".DE",
    "GY": ".DE",
    "SW": ".SW",
    "SE": ".SW",
    "NA": ".AS",
    "IM": ".MI",
    "SM": ".MC",
    "SQ": ".MC",
    "BB": ".BR",
    "ID": ".IR",
    "DC": ".CO",
    "NO": ".OL",
    "AV": ".VI",
    "PL": ".LS",
    "FH": ".HE",
    "CN": ".TO",
    "HK": ".HK",
    "JP": ".T",
    "SS": ".ST",
    "AU": ".AX",
}

# Bloomberg exchange code -> ISO 4217 local trading currency.
BLOOMBERG_EXCHANGE_TO_CURRENCY: dict[str, str] = {
    "US": "USD", "UW": "USD", "UN": "USD",   # US exchanges
    "UA": "USD",                              # NYSE American / Arca-style US listings
    "LN": "GBP",                              # London (pence -> GBP via yfinance)
    "FP": "EUR",                              # Euronext Paris
    "GR": "EUR", "GY": "EUR",                # Xetra / Frankfurt
    "SW": "CHF",                              # SIX Swiss Exchange
    "SE": "CHF",                              # Legacy SIX alias in Bloomberg exports
    "NA": "EUR",                              # Euronext Amsterdam
    "IM": "EUR",                              # Borsa Italiana
    "SM": "EUR",                              # BME Madrid
    "SQ": "EUR",                              # BME Madrid legacy alias
    "BB": "EUR",                              # Euronext Brussels
    "ID": "EUR",                              # Euronext Dublin / Irish listings
    "AV": "EUR",                              # Vienna
    "DC": "DKK",                              # Nasdaq Copenhagen
    "FH": "EUR",                              # Nasdaq Helsinki
    "CN": "CAD",                              # Toronto
    "HK": "HKD",                              # Hong Kong
    "JP": "JPY",                              # Tokyo
    "KS": "KRW",                              # Korea Exchange
    "SS": "SEK",                              # Nasdaq Stockholm
    "NO": "NOK",                              # Oslo
    "PL": "EUR",                              # Euronext Lisbon
    "AU": "AUD",                              # ASX
    "EQUITY": "USD",                          # Missing explicit exchange; most such names are US
}

# Manual overrides where the Bloomberg -> Yahoo mapping is not 1:1.
# Union of the former MSARN and Companies_News tables.
YAHOO_TICKER_OVERRIDES: dict[str, str] = {
    "AAXN": "AXON",
    "ABC": "COR",
    "ASSAB": "ASSA-B.ST",
    "ATCOA": "ATCO-A.ST",
    "BRKB": "BRK-B",
    # Bare "CFR" on Yahoo resolves to Cullen/Frost Bankers (US); pin to Richemont.
    "CFR": "CFR.SW",
    "EDPR": "EDPR.LS",
    "EPIA": "EPI-A.ST",
    "GLB": "GL9.IR",
    "KYGA": "KRZ.IR",
    "LHN": "HOLN.SW",
    "NIBEB": "NIBE-B.ST",
    "NOVOB": "NOVO-B.CO",
    "ROCKB": "ROCK-B.CO",
    "SCAA": "SCA-A.ST",
    "SCAB": "SCA-B.ST",
    "VITB": "VIT-B.ST",
}


def normalize_ticker(raw: str) -> str:
    """Normalize ticker-like text, e.g. ``"AAPL US Equity" -> "AAPL"``."""
    text = (raw or "").strip().upper()
    return text.split()[0] if text else ""


def parse_bloomberg_to_yahoo_symbol(bbg_ticker: str) -> str:
    """Convert a Bloomberg composite ticker to a Yahoo Finance symbol.

    Examples:
      "IXN US Equity"   -> "IXN"
      "NOVN SW Equity"  -> "NOVN.SW"
      "MC FP Equity"    -> "MC.PA"
      "BRK/B US Equity" -> "BRK-B"
      "UU/ LN Equity"   -> "UU.L"
    """
    if not bbg_ticker:
        return ""
    parts = str(bbg_ticker).strip().upper().split()
    if not parts:
        return ""
    # Bloomberg sometimes uses slash notation for share classes or exchange-
    # specific shorthands. Yahoo expects either "-" (e.g. BRK-B) or no slash
    # at all (e.g. UU.L), so normalize at the adapter boundary.
    symbol = parts[0].rstrip("/").replace("/", "-")
    if len(parts) < 2:
        return symbol

    exch = parts[1]
    if exch == "HK" and symbol.isdigit():
        # Yahoo requires 4-digit zero-padded HK codes (e.g. "700" -> "0700.HK").
        symbol = symbol.zfill(4)

    suffix = BLOOMBERG_EXCHANGE_TO_YAHOO_SUFFIX.get(exch, "")
    return f"{symbol}{suffix}"


def derive_yahoo_symbol(ticker: str, bloomberg_ticker: str = "") -> str:
    """Return the Yahoo-compatible market symbol for one canonical ticker.

    Priority: manual override -> already-dotted canonical passthrough ->
    Bloomberg-derived -> bare canonical. The canonical ticker stays stable for
    internal joins; only the vendor adapter layer translates.
    """
    canonical = normalize_ticker(ticker)
    if not canonical:
        return ""

    explicit = YAHOO_TICKER_OVERRIDES.get(canonical)
    if explicit:
        return explicit

    if "." in canonical:
        return canonical

    from_bloomberg = parse_bloomberg_to_yahoo_symbol(bloomberg_ticker)
    return from_bloomberg or canonical


def parse_bloomberg_to_currency(bbg: str) -> str:
    """Derive ISO 4217 from a Bloomberg composite ticker (e.g. ``'NOVN SW Equity'`` -> ``'CHF'``).

    Used as fallback when an explicit currency column is missing.
    """
    if not bbg:
        return "USD"
    parts = str(bbg).strip().upper().split()
    exch = parts[1] if len(parts) >= 2 else ""
    return BLOOMBERG_EXCHANGE_TO_CURRENCY.get(exch, "USD")


@lru_cache(maxsize=1)
def load_registry() -> dict[str, TickerInfo]:
    """Read every row of ``market.ticker_metadata`` into memory.

    Cached for the lifetime of the process. Call :func:`reload_registry`
    after a universe ingest run.

    Note: this reads the DB *mirror*. During the transition phase, consumer
    runtime remains CSV-authoritative (each repo's generated ``tickers.csv``);
    this registry serves tooling, drift checks, and validation.
    """
    from research_commons.db_market.reads import load_ticker_metadata

    df = load_ticker_metadata()
    registry: dict[str, TickerInfo] = {}
    for row in df.to_dict(orient="records"):
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        registry[ticker] = TickerInfo(
            ticker=ticker,
            company_name=str(row.get("company_name") or ""),
            isin=(str(row["isin"]).strip() or None) if row.get("isin") is not None else None,
            bloomberg_ticker=(
                (str(row["bloomberg_ticker"]).strip() or None)
                if row.get("bloomberg_ticker") is not None
                else None
            ),
            yahoo_symbol=str(row.get("yahoo_symbol") or "").strip(),
            sector_name=(
                (str(row["sector_name"]).strip() or None)
                if row.get("sector_name") is not None
                else None
            ),
            currency=str(row.get("currency") or "").strip().upper(),
            scope_status=str(row.get("scope_status") or "inactive").strip().lower(),
        )
    return registry


def reload_registry() -> None:
    """Drop the in-process cache so the next access re-reads the DB."""
    load_registry.cache_clear()


def get(ticker: str) -> TickerInfo:
    """Return the :class:`TickerInfo` for ``ticker`` or raise ``KeyError``."""
    return load_registry()[normalize_ticker(ticker)]


def get_currency(ticker: str) -> str:
    """Return the ISO 4217 trading currency for ``ticker``."""
    return get(ticker).currency
