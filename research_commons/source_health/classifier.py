"""Map raw HTTP observations to stable source-health statuses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal
from urllib.parse import urlparse

from research_commons.source_health.checker import HTTPCheckResult

HealthStatus = Literal["WORKING", "ACCESSIBLE_NO_CONTENT", "BLOCKED", "ERROR"]

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


@dataclass(frozen=True)
class SourceHealthRecord:
    """Final row shape stored in the `source_health` table."""

    source_url: str
    status: HealthStatus
    http_status: int | None
    response_time: float | None
    last_checked: datetime
    notes: str


def classify_result(
    raw: HTTPCheckResult,
    *,
    checked_at: datetime | None = None,
    extra_keywords: tuple[str, ...] = (),
) -> SourceHealthRecord:
    """Convert a raw HTTP observation into one of four stable statuses."""

    timestamp = checked_at or datetime.now(timezone.utc)

    if raw.robots_disallowed:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="BLOCKED",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=raw.note or "robots.txt blocks homepage checks",
        )

    if raw.error is not None:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="ERROR",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=raw.error,
        )

    if raw.http_status in {401, 403}:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="BLOCKED",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=f"HTTP {raw.http_status} blocks homepage access",
        )

    if raw.bot_protection_detected:
        return SourceHealthRecord(
            source_url=raw.source_url,
            status="BLOCKED",
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes="Bot protection or challenge page detected",
        )

    if raw.http_status == 200:
        summary = _content_summary(raw.body_text, raw.final_url or raw.checked_url, extra_keywords)
        status: HealthStatus
        if summary.is_meaningful:
            status = "WORKING"
            notes = (
                f"meaningful content: chars={summary.visible_chars}, "
                f"words={summary.word_count}, keyword_hits={summary.keyword_hits}"
            )
        else:
            status = "ACCESSIBLE_NO_CONTENT"
            notes = (
                f"accessible but weak content: chars={summary.visible_chars}, "
                f"words={summary.word_count}, keyword_hits={summary.keyword_hits}"
            )
        return SourceHealthRecord(
            source_url=raw.source_url,
            status=status,
            http_status=raw.http_status,
            response_time=raw.response_time,
            last_checked=timestamp,
            notes=notes,
        )

    return SourceHealthRecord(
        source_url=raw.source_url,
        status="ERROR",
        http_status=raw.http_status,
        response_time=raw.response_time,
        last_checked=timestamp,
        notes=f"Unexpected HTTP status {raw.http_status}",
    )


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
