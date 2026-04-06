"""Canonical ticker registry.

This module is the *single source of truth* for which logical ticker symbols
exist in the ecosystem. The CSV behind it lives in DB1 (`ticker_metadata`
table). Project-specific ``tickers.csv`` files become legacy after migration.

Helpers here normalize Yahoo Finance / Bloomberg / ISIN identifiers and map a
ticker to its local trading currency (ISO 4217).
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
    sector: str | None
    currency: str        # ISO 4217, e.g. "CHF"


@lru_cache(maxsize=1)
def load_registry() -> dict[str, TickerInfo]:
    """Read every row of `market.ticker_metadata` into memory.

    Cached for the lifetime of the process. Call :func:`reload_registry`
    after a tickers.csv sync run.
    """
    raise NotImplementedError("Stub. SELECT * FROM ticker_metadata.")


def reload_registry() -> None:
    """Drop the in-process cache so the next access re-reads the DB."""
    load_registry.cache_clear()


def get(ticker: str) -> TickerInfo:
    """Return the :class:`TickerInfo` for ``ticker`` or raise ``KeyError``."""
    raise NotImplementedError("Stub. Use load_registry()[ticker.upper()].")


def get_currency(ticker: str) -> str:
    """Return the ISO 4217 trading currency for ``ticker``."""
    raise NotImplementedError("Stub. Return get(ticker).currency.")


def parse_bloomberg_to_currency(bbg: str) -> str:
    """Derive ISO 4217 from a Bloomberg suffix (e.g. ``'NOVN SW'`` → ``'CHF'``).

    Used as fallback when an explicit currency column is missing.
    """
    raise NotImplementedError("Stub. Map exchange code → ISO 4217.")
