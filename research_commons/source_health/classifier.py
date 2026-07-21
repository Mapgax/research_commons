"""Map raw HTTP observations to stable source-health statuses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal
from urllib.parse import urlparse

from research_commons.source_health.checker import HTTPCheckResult

HealthStatus = Literal[
    "OK",
    "STALE",
    "PARSE_FAILED",
    "BLOCKED_ROBOTS",
    "BLOCKED_HTTP",
    "BLOCKED_CHALLENGE",
    "UNREACHABLE",
]

DEFAULT_STALE_THRESHOLD_DAYS = 14

_COMMON_HOST_TOKENS = {
    "www",
    "com",
    "org",
    "net",
    "co",
    "io",
    "gov",
    "www2",
    "rss",
    "api",
}


SUCCESS_STATUSES: frozenset[str] = frozenset({"OK", "STALE"})
FAILURE_STATUSES: frozenset[str] = frozenset(
    {"PARSE_FAILED", "BLOCKED_ROBOTS", "BLOCKED_HTTP", "BLOCKED_CHALLENGE", "UNREACHABLE"}
)

_SUGGESTED_ACTIONS: dict[str, str] = {
    "robots_disallowed": "Check whether robots.txt now blocks this homepage; consider an alternate entry URL.",
    "request_error": "Verify the URL still resolves; check for DNS or TLS changes.",
    "http_blocked": "Site is actively blocking requests (401/403); consider rotating user agent or marking inactive.",
    "bot_protection": "Bot/challenge page detected; scraping this source likely needs a different approach or should be deprioritized.",
    "unexpected_status": "Unexpected HTTP status; inspect manually and confirm the homepage URL is still correct.",
    "no_meaningful_content": "Homepage loads but has little extractable text; confirm the URL points at the right page.",
    "stale": "Homepage is reachable again but had no successful check in a while; verify scraped content looks right before trusting it fully.",
}


def _suggested_action(failure_reason: str | None) -> str | None:
    if failure_reason is None:
        return None
    return _SUGGESTED_ACTIONS.get(failure_reason)


@dataclass(frozen=True)
class SourceHealthRecord:
    """Final row shape stored in the `source_health` table."""

    source_url: str
    status: HealthStatus
    http_status: int | None
    response_time: float | None
    last_checked: datetime
    notes: str
    failure_reason: str | None = None
    failure_detail: str | None = None
    consecutive_failures: int = 0
    suggested_action: str | None = None


def classify_result(
    raw: HTTPCheckResult,
    *,
    checked_at: datetime | None = None,
    extra_keywords: tuple[str, ...] = (),
    previous_consecutive_failures: int = 0,
    previous_last_ok_at: datetime | None = None,
    stale_threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS,
) -> SourceHealthRecord:
    """Convert a raw HTTP observation into one of seven stable statuses.

    Waterfall: robots -> request error -> HTTP block -> challenge page ->
    body parse -> freshness -> OK.

    `previous_consecutive_failures` carries the prior run's streak so it can be
    incremented on another failure or reset to 0 on success (J6). `previous_last_ok_at`
    and `stale_threshold_days` decide whether a passing check is reported as OK or as
    STALE — a source that is reachable again after a long gap without a successful
    check (J9), surfaced so it gets a second look before being trusted again.
    """

    timestamp = checked_at or datetime.now(timezone.utc)

    if raw.robots_disallowed:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="BLOCKED_ROBOTS",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=raw.note or "robots.txt blocks homepage checks",
            failure_reason="robots_disallowed",
            failure_detail=raw.failure_detail or raw.note,
            consecutive_failures=previous_consecutive_failures + 1,
            suggested_action=_suggested_action("robots_disallowed"),
        )

    if raw.error is not None:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="UNREACHABLE",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=raw.error,
            failure_reason="request_error",
            failure_detail=raw.failure_detail or raw.error,
            consecutive_failures=previous_consecutive_failures + 1,
            suggested_action=_suggested_action("request_error"),
        )

    if raw.http_status in {401, 403}:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="BLOCKED_HTTP",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=f"HTTP {raw.http_status} blocks homepage access",
            failure_reason="http_blocked",
            failure_detail=f"HTTP {raw.http_status}",
            consecutive_failures=previous_consecutive_failures + 1,
            suggested_action=_suggested_action("http_blocked"),
        )

    if raw.bot_protection_detected:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="BLOCKED_CHALLENGE",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes="Bot protection or challenge page detected",
            failure_reason="bot_protection",
            failure_detail=raw.failure_detail or raw.note,
            consecutive_failures=previous_consecutive_failures + 1,
            suggested_action=_suggested_action("bot_protection"),
        )

    if raw.http_status == 200:
        summary = _content_summary(raw.body_text, raw.final_url or raw.checked_url, extra_keywords)

        if not summary.is_meaningful:
            notes = (
                f"accessible but weak content: chars={summary.visible_chars}, "
                f"words={summary.word_count}, keyword_hits={summary.keyword_hits}"
            )
            return SourceHealthRecord(
                source_url=raw.source_url,
                status="PARSE_FAILED",
                http_status=raw.http_status,
                response_time=raw.response_time,
                last_checked=timestamp,
                notes=notes,
                failure_reason="no_meaningful_content",
                consecutive_failures=previous_consecutive_failures + 1,
                suggested_action=_suggested_action("no_meaningful_content"),
            )

        notes = (
            f"meaningful content: chars={summary.visible_chars}, "
            f"words={summary.word_count}, keyword_hits={summary.keyword_hits}"
        )
        if _is_stale(previous_last_ok_at, timestamp, stale_threshold_days):
            return SourceHealthRecord(
                source_url=raw.source_url,
                status="STALE",
                http_status=raw.http_status,
                response_time=raw.response_time,
                last_checked=timestamp,
                notes=f"{notes}; no successful check in over {stale_threshold_days}d before this one",
                failure_reason="stale",
                consecutive_failures=0,
                suggested_action=_suggested_action("stale"),
            )

        return SourceHealthRecord(
            source_url=raw.source_url,
            status="OK",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=notes,
            consecutive_failures=0,
        )

    return SourceHealthRecord(
        source_url=raw.source_url,
        status="UNREACHABLE",
        http_status=raw.http_status,
        response_time=raw.response_time,
        last_checked=timestamp,
        notes=f"Unexpected HTTP status {raw.http_status}",
        failure_reason="unexpected_status",
        failure_detail=f"HTTP {raw.http_status}",
        consecutive_failures=previous_consecutive_failures + 1,
        suggested_action=_suggested_action("unexpected_status"),
    )


def _is_stale(
    previous_last_ok_at: datetime | None,
    checked_at: datetime,
    stale_threshold_days: int,
) -> bool:
    """A source is STALE if it has a known last-OK time that is now too old.

    A source with no recorded last-OK time (new, or never seen passing before)
    is not STALE — it's simply OK on its first observed success.
    """
    if previous_last_ok_at is None:
        return False
    return (checked_at - previous_last_ok_at).days > stale_threshold_days


@dataclass(frozen=True)
class _ContentSummary:
    visible_chars: int
    word_count: int
    keyword_hits: int
    is_meaningful: bool


def _content_summary(body_text: str, source_url: str, extra_keywords: tuple[str, ...]) -> _ContentSummary:
    visible_text = _visible_text(body_text)
    visible_chars = len(visible_text)
    word_count = len(re.findall(r"[A-Za-z]{2,}", visible_text))

    haystack = visible_text.lower()
    keywords = _expected_keywords(source_url, extra_keywords)
    keyword_hits = sum(1 for keyword in keywords if keyword in haystack)

    is_meaningful = visible_chars >= 200 and (word_count >= 40 or keyword_hits >= 1)
    return _ContentSummary(
        visible_chars=visible_chars,
        word_count=word_count,
        keyword_hits=keyword_hits,
        is_meaningful=is_meaningful,
    )


def _visible_text(body_text: str) -> str:
    without_scripts = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        body_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    collapsed = re.sub(r"\s+", " ", without_tags).strip()
    return collapsed[:10_000]


def _expected_keywords(source_url: str, extra_keywords: tuple[str, ...]) -> tuple[str, ...]:
    hostname = urlparse(source_url).hostname or ""
    host_tokens = tuple(
        token
        for token in re.split(r"[^a-z0-9]+", hostname.lower())
        if len(token) >= 4 and token not in _COMMON_HOST_TOKENS
    )
    merged = {token for token in host_tokens}
    merged.update(keyword.lower() for keyword in extra_keywords if keyword.strip())
    return tuple(sorted(merged))
