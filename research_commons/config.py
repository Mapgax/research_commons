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


@dataclass(frozen=True)
class SourceHealthSettings:
    """Configuration for the independent homepage health monitor."""

    news_database_url: str
    log_level: str = "INFO"

    source_health_timeout_sec: float = 8.0
    source_health_max_retries: int = 2
    source_health_user_agent: str = "research-commons-source-health/0.1"
    source_health_report_dir: str = "reports/source_health"
    source_health_keywords: tuple[str, ...] = ()

    resend_api_key: str | None = None
    resend_from: str | None = None
    email_to: tuple[str, ...] = ()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"See README.md in the research_commons repo for the full list."
        )
    return val


def _parse_csv_env(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


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
        market_database_url=os.getenv("MARKET_DATABASE_URL", ""),
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


@lru_cache(maxsize=1)
def get_source_health_settings() -> SourceHealthSettings:
    """Return config for the source-health monitor.

    This path is intentionally lighter than :func:`get_settings`: the health
    monitor only needs `NEWS_DATABASE_URL`, so it stays runnable even if
    `MARKET_DATABASE_URL` is not configured in an isolated GitHub Action.
    """
    return SourceHealthSettings(
        news_database_url=_require("NEWS_DATABASE_URL"),
        log_level=os.getenv("RC_LOG_LEVEL", "INFO"),
        source_health_timeout_sec=float(os.getenv("SOURCE_HEALTH_TIMEOUT_SEC", "8")),
        source_health_max_retries=int(os.getenv("SOURCE_HEALTH_MAX_RETRIES", "2")),
        source_health_user_agent=os.getenv(
            "SOURCE_HEALTH_USER_AGENT",
            "research-commons-source-health/0.1",
        ),
        source_health_report_dir=os.getenv(
            "SOURCE_HEALTH_REPORT_DIR",
            "reports/source_health",
        ),
        source_health_keywords=_parse_csv_env("SOURCE_HEALTH_KEYWORDS"),
        resend_api_key=os.getenv("RESEND_API_KEY"),
        resend_from=os.getenv("RESEND_FROM"),
        email_to=_parse_csv_env("EMAIL_TO"),
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
