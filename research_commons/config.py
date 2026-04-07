"""Central environment-variable loader.

All other modules in `research_commons` MUST import settings from here rather
than calling ``os.getenv`` directly. This way the projects share one consistent
view of what is configured and what isn't.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

# Load .env from cwd at import time. Safe no-op if no file exists.
load_dotenv()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    """Frozen runtime configuration. Build via :func:`get_settings`."""

    market_database_url: str
    news_database_url: str

    anthropic_api_key: str | None
    gemini_api_key: str | None

    llm_primary_model: str = "claude-haiku-4-5-20251001"
    llm_fallback_model: str = "gemini-2.5-flash"

    log_level: str = "INFO"

    # Connection-pool sizing — kept conservative; bump per-project if needed.
    market_pool_min: int = 1
    market_pool_max: int = 5
    news_pool_min: int = 1
    news_pool_max: int = 5

    # Optional Postgres role overrides — leave None to use the URL's user.
    market_role: str | None = None
    news_role: str | None = None

    extras: dict[str, str] = field(default_factory=dict)


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"See README.md in the research_commons repo for the full list."
        )
    return val


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Raises ``RuntimeError`` if a *required* variable is missing.

    The function is ``@lru_cache``-d, which means the first call builds the
    object and every subsequent call returns the same instance without reading
    environment variables again. This is intentional: config is expected to be
    stable for the lifetime of a process. In tests, call
    ``get_settings.cache_clear()`` before each test that needs a different env.
    """
    return Settings(
        market_database_url=_require("MARKET_DATABASE_URL"),
        news_database_url=_require("NEWS_DATABASE_URL"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        llm_primary_model=os.getenv("LLM_PRIMARY_MODEL", "claude-haiku-4-5-20251001"),
        llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", "gemini-2.5-flash"),
        log_level=os.getenv("RC_LOG_LEVEL", "INFO"),
        market_pool_min=int(os.getenv("MARKET_POOL_MIN", "1")),
        market_pool_max=int(os.getenv("MARKET_POOL_MAX", "5")),
        news_pool_min=int(os.getenv("NEWS_POOL_MIN", "1")),
        news_pool_max=int(os.getenv("NEWS_POOL_MAX", "5")),
        market_role=os.getenv("MARKET_ROLE"),
        news_role=os.getenv("NEWS_ROLE"),
    )


def configure_logging(level: str | None = None) -> None:
    """Apply a sane default logging config. Idempotent.

    Safe to call multiple times; ``logging.basicConfig`` is a no-op if the
    root logger already has handlers attached.
    """
    effective_level = level or os.getenv("RC_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=effective_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
