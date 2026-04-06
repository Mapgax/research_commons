"""Static metadata about the news/data sources we ingest from.

Lives in code (not the DB) so it can be code-reviewed and version-controlled.
A nightly job mirrors this dict into the DB2 ``sources`` table for FK integrity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SourceTier = Literal["primary", "aggregator", "rss", "regulatory", "social"]


@dataclass(frozen=True)
class SourceMeta:
    name: str               # canonical key, lowercase, no spaces (e.g. "newsapi")
    display_name: str
    tier: SourceTier
    base_url: str
    requires_api_key: bool
    rate_limit_per_min: int | None
    notes: str = ""


KNOWN_SOURCES: dict[str, SourceMeta] = {
    "newsapi": SourceMeta(
        name="newsapi",
        display_name="NewsAPI.org",
        tier="aggregator",
        base_url="https://newsapi.org/v2/",
        requires_api_key=True,
        rate_limit_per_min=100,
        notes="Used by MSARN and Companies_News. Free tier capped.",
    ),
    "gdelt": SourceMeta(
        name="gdelt",
        display_name="GDELT Project",
        tier="aggregator",
        base_url="https://api.gdeltproject.org/",
        requires_api_key=False,
        rate_limit_per_min=None,
        notes="Free; BigQuery variant requires GCP credentials.",
    ),
    "finnhub": SourceMeta(
        name="finnhub",
        display_name="Finnhub Stock News",
        tier="primary",
        base_url="https://finnhub.io/api/v1/",
        requires_api_key=True,
        rate_limit_per_min=60,
    ),
    "google_news_rss": SourceMeta(
        name="google_news_rss",
        display_name="Google News RSS",
        tier="rss",
        base_url="https://news.google.com/rss",
        requires_api_key=False,
        rate_limit_per_min=None,
    ),
    "edgar": SourceMeta(
        name="edgar",
        display_name="SEC EDGAR",
        tier="regulatory",
        base_url="https://www.sec.gov/cgi-bin/browse-edgar",
        requires_api_key=False,
        rate_limit_per_min=600,
        notes="Requires User-Agent header with contact email.",
    ),
    "manual": SourceMeta(
        name="manual",
        display_name="Manual / Pasted",
        tier="primary",
        base_url="",
        requires_api_key=False,
        rate_limit_per_min=None,
    ),
    "scraper": SourceMeta(
        name="scraper",
        display_name="Idee_Scraping web scrapers",
        tier="primary",
        base_url="",
        requires_api_key=False,
        rate_limit_per_min=None,
    ),
}


def get_source(name: str) -> SourceMeta:
    """Lookup by canonical name; raises ``KeyError`` if unknown."""
    return KNOWN_SOURCES[name]
