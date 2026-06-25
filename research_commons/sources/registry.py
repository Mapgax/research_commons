"""Static metadata about the news/data sources we ingest from.

Lives in code (not the DB) so it can be code-reviewed and version-controlled.
``Companies_News/news_db_sync.py::ensure_known_news_sources_registered()`` and
``investment_idea_scapper/investment_radar/scrapers/registry.py::seed_sources()``
both upsert this dict into the DB2 ``sources`` table on each pipeline run, so
``articles.source`` FK inserts never fail on an unregistered source name.

Scope note (J15): this registry owns a small set of API/aggregator sources
(NewsAPI, GDELT, Finnhub, EDGAR, manual, scraper) shared across all three
repos. It is intentionally separate from
``investment_idea_scapper/investment_radar/scrapers/registry.py``, which owns
the 40+ web-scraper sources consumed by investment_radar's own collector
pipeline (RSS/static-HTML/sitemap scraping). Both registries write into the
same ``news_db.sources`` table by design.
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
