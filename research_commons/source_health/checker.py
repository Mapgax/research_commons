"""Direct, lightweight homepage health checks.

Design note:
    This module is intentionally independent from any scraper or parsing
    pipeline. It performs only:
    1. a `robots.txt` permission check,
    2. a small `GET` request against the homepage URL,
    3. minimal bot-protection heuristics.

Security / external data flow:
    Running this module sends HTTP requests to third-party sites. Only the
    request URL and basic headers (notably the User-Agent) leave your machine.
    No article content, API keys, or scraped payloads are uploaded here.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

_BOT_PROTECTION_MARKERS = (
    "attention required",
    "captcha",
    "cf-chl",
    "cloudflare",
    "access denied",
    "bot verification",
    "please verify you are human",
)


@dataclass(frozen=True)
class HTTPCheckResult:
    """Raw HTTP-layer observation before status classification."""

    source_url: str
    checked_url: str
    http_status: int | None
    response_time: float | None
    body_text: str
    final_url: str | None
    robots_disallowed: bool = False
    bot_protection_detected: bool = False
    error: str | None = None
    note: str = ""


class HomepageChecker:
    """Small wrapper around `httpx.Client` with robots-aware retries."""

    def __init__(
        self,
        *,
        timeout_sec: float = 8.0,
        max_retries: int = 2,
        user_agent: str = "research-commons-source-health/0.1",
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_sec, follow_redirects=True)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> HomepageChecker:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def check_many(self, source_urls: list[str]) -> list[HTTPCheckResult]:
        return [self.check_url(source_url) for source_url in source_urls]

    def check_url(self, source_url: str) -> HTTPCheckResult:
        checked_url = _normalise_url(source_url)
        if not checked_url:
            return HTTPCheckResult(
                source_url=source_url,
                checked_url=source_url,
                http_status=None,
                response_time=None,
                body_text="",
                final_url=None,
                error="Invalid or empty URL",
            )

        robots_note = self._check_robots(checked_url)
        if robots_note is not None:
            return HTTPCheckResult(
                source_url=source_url,
                checked_url=checked_url,
                http_status=None,
                response_time=None,
                body_text="",
                final_url=None,
                robots_disallowed=True,
                note=robots_note,
            )

        last_error: str | None = None
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            started = time.perf_counter()
            try:
                response = self.client.get(
                    checked_url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                    },
                )
                elapsed = time.perf_counter() - started
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Homepage check failed for %s (attempt %d/%d): %s",
                    checked_url,
                    attempt,
                    total_attempts,
                    last_error,
                )
                if attempt == total_attempts:
                    return HTTPCheckResult(
                        source_url=source_url,
                        checked_url=checked_url,
                        http_status=None,
                        response_time=None,
                        body_text="",
                        final_url=None,
                        error=last_error,
                    )
                continue

            if response.status_code >= 500 and attempt < total_attempts:
                last_error = f"Server error {response.status_code}"
                logger.warning(
                    "Retrying %s after %s (attempt %d/%d)",
                    checked_url,
                    last_error,
                    attempt,
                    total_attempts,
                )
                continue

            body_text = response.text[:50_000]
            return HTTPCheckResult(
                source_url=source_url,
                checked_url=checked_url,
                http_status=response.status_code,
                response_time=elapsed,
                body_text=body_text,
                final_url=str(response.url),
                bot_protection_detected=_looks_like_bot_protection(response, body_text),
                note="",
            )

        return HTTPCheckResult(
            source_url=source_url,
            checked_url=checked_url,
            http_status=None,
            response_time=None,
            body_text="",
            final_url=None,
            error=last_error or "Unknown error",
        )

    def _check_robots(self, checked_url: str) -> str | None:
        robots_url = urljoin(checked_url, "/robots.txt")
        try:
            response = self.client.get(
                robots_url,
                headers={"User-Agent": self.user_agent, "Accept": "text/plain,*/*;q=0.1"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.info("robots.txt unavailable for %s: %s", checked_url, exc)
            return None

        if response.status_code != 200 or not response.text.strip():
            return None

        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())

        if not parser.can_fetch(self.user_agent, checked_url):
            return f"robots.txt disallows {checked_url} for {self.user_agent}"
        return None


def _normalise_url(source_url: str) -> str:
    trimmed = source_url.strip()
    if not trimmed:
        return ""
    parsed = urlparse(trimmed)
    if parsed.scheme:
        return trimmed
    return f"https://{trimmed}"


def _looks_like_bot_protection(response: httpx.Response, body_text: str) -> bool:
    if response.status_code == 429:
        return True

    server = response.headers.get("server", "").lower()
    if "cloudflare" in server:
        return True

    if response.headers.get("cf-mitigated"):
        return True

    haystack = f"{server}\n{body_text.lower()[:5_000]}"
    return any(marker in haystack for marker in _BOT_PROTECTION_MARKERS)
