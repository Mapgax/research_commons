"""Database IO and weekly report generation for source-health snapshots."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import logging

from research_commons.db_news import get_connection
from research_commons.source_health.classifier import (
    DEFAULT_STALE_THRESHOLD_DAYS,
    SUCCESS_STATUSES,
    SourceHealthRecord,
)

logger = logging.getLogger(__name__)

CIRCUIT_OPEN_THRESHOLD = 3


@dataclass(frozen=True)
class PreviousHealthRecord:
    source_url: str
    status: str
    http_status: int | None
    response_time: float | None
    last_checked: datetime
    notes: str
    consecutive_failures: int = 0


@dataclass(frozen=True)
class SourceFreshnessState:
    last_ok_at: datetime | None
    stale_threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS


@dataclass(frozen=True)
class SourceHealthRunSummary:
    checked_urls: list[str]
    records: list[SourceHealthRecord]
    report_path: Path
    email_sent: bool
    markdown_report: str


def load_source_urls() -> list[str]:
    """Load homepage URLs from the shared `sources` table.

    We prefer `sources.url` when present (Idee_Scraping compatibility field),
    otherwise fall back to `sources.base_url` from the canonical registry.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'sources'
                ORDER BY column_name
                """
            )
            columns = {row[0] for row in cur.fetchall()}

            url_expr_parts: list[str] = []
            if "url" in columns:
                url_expr_parts.append("NULLIF(url, '')")
            if "base_url" in columns:
                url_expr_parts.append("NULLIF(base_url, '')")

            if not url_expr_parts:
                return []

            source_url_expr = f"COALESCE({', '.join(url_expr_parts)})"
            where_parts = [f"{source_url_expr} IS NOT NULL"]
            if "active" in columns:
                where_parts.append("COALESCE(active, TRUE)")

            cur.execute(
                f"""
                SELECT DISTINCT {source_url_expr} AS source_url
                FROM sources
                WHERE {' AND '.join(where_parts)}
                ORDER BY source_url
                """
            )
            rows = [row[0] for row in cur.fetchall()]

    return [url for url in rows if isinstance(url, str) and url.startswith(("http://", "https://"))]


def load_previous_records(source_urls: Iterable[str]) -> dict[str, PreviousHealthRecord]:
    ordered_urls = list(dict.fromkeys(source_urls))
    if not ordered_urls:
        return {}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (source_url)
                    source_url,
                    status,
                    http_status,
                    response_time,
                    last_checked,
                    notes,
                    consecutive_failures
                FROM source_health
                WHERE source_url = ANY(%s)
                ORDER BY source_url, last_checked DESC, id DESC
                """,
                (ordered_urls,),
            )
            rows = cur.fetchall()

    previous: dict[str, PreviousHealthRecord] = {}
    for source_url, status, http_status, response_time, last_checked, notes, consecutive_failures in rows:
        previous[source_url] = PreviousHealthRecord(
            source_url=source_url,
            status=status,
            http_status=http_status,
            response_time=response_time,
            last_checked=last_checked,
            notes=notes or "",
            consecutive_failures=consecutive_failures or 0,
        )
    return previous


def _sources_columns(cur) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'sources'
        """
    )
    return {row[0] for row in cur.fetchall()}


def _sources_url_match_expr(columns: set[str]) -> str | None:
    match_parts = []
    if "url" in columns:
        match_parts.append("url = %(source_url)s")
    if "base_url" in columns:
        match_parts.append("base_url = %(source_url)s")
    if not match_parts:
        return None
    return " OR ".join(match_parts)


def load_source_freshness_state(source_urls: Iterable[str]) -> dict[str, SourceFreshnessState]:
    """Load each source's last successful health check + staleness window (J9).

    Falls back to `DEFAULT_STALE_THRESHOLD_DAYS` when the column is absent or
    unset, and returns no entry for a URL the `sources` table doesn't recognize.
    """
    ordered_urls = list(dict.fromkeys(source_urls))
    if not ordered_urls:
        return {}

    with get_connection() as conn:
        with conn.cursor() as cur:
            columns = _sources_columns(cur)
            match_expr = _sources_url_match_expr(columns)
            if match_expr is None or "last_ok_at" not in columns:
                return {}

            select_url_parts = []
            if "url" in columns:
                select_url_parts.append("NULLIF(url, '')")
            if "base_url" in columns:
                select_url_parts.append("NULLIF(base_url, '')")
            source_url_expr = f"COALESCE({', '.join(select_url_parts)})"

            stale_column = "stale_threshold_days" if "stale_threshold_days" in columns else "NULL"
            cur.execute(
                f"""
                SELECT {source_url_expr} AS source_url, last_ok_at, {stale_column} AS stale_threshold_days
                FROM sources
                WHERE {source_url_expr} = ANY(%(urls)s)
                """,
                {"urls": ordered_urls},
            )
            rows = cur.fetchall()

    return {
        source_url: SourceFreshnessState(
            last_ok_at=last_ok_at,
            stale_threshold_days=stale_threshold_days or DEFAULT_STALE_THRESHOLD_DAYS,
        )
        for source_url, last_ok_at, stale_threshold_days in rows
    }


def update_circuit_breaker_state(records: list[SourceHealthRecord], *, checked_at: datetime) -> None:
    """Persist circuit_open / last_ok_at on `sources` after a health run (J9).

    circuit_open is fully derived from this run's consecutive_failures count —
    it opens at `CIRCUIT_OPEN_THRESHOLD` and clears the moment a check succeeds.
    """
    if not records:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            columns = _sources_columns(cur)
            match_expr = _sources_url_match_expr(columns)
            if match_expr is None or "circuit_open" not in columns:
                return

            set_last_ok_at = "last_ok_at" in columns
            for record in records:
                is_success = record.status in SUCCESS_STATUSES
                set_clause = "circuit_open = %(circuit_open)s"
                if set_last_ok_at:
                    set_clause += ", last_ok_at = CASE WHEN %(is_success)s THEN %(checked_at)s ELSE last_ok_at END"
                cur.execute(
                    f"UPDATE sources SET {set_clause} WHERE {match_expr}",
                    {
                        "circuit_open": record.consecutive_failures >= CIRCUIT_OPEN_THRESHOLD,
                        "is_success": is_success,
                        "checked_at": checked_at,
                        "source_url": record.source_url,
                    },
                )
        conn.commit()


def insert_health_records(records: list[SourceHealthRecord]) -> int:
    if not records:
        return 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO source_health (
                    source_url,
                    status,
                    http_status,
                    response_time,
                    last_checked,
                    notes,
                    failure_reason,
                    failure_detail,
                    consecutive_failures,
                    suggested_action
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        record.source_url,
                        record.status,
                        record.http_status,
                        record.response_time,
                        record.last_checked,
                        record.notes,
                        record.failure_reason,
                        record.failure_detail,
                        record.consecutive_failures,
                        record.suggested_action,
                    )
                    for record in records
                ],
            )
        conn.commit()
    return len(records)


def build_weekly_report(
    records: list[SourceHealthRecord],
    previous_by_url: dict[str, PreviousHealthRecord],
    *,
    generated_at: datetime | None = None,
) -> str:
    """Render the J13 four-section report: Action Required, Degraded, Monitoring, Healthy."""
    timestamp = generated_at or datetime.now(timezone.utc)
    counts = Counter(record.status for record in records)
    ordered_records = sorted(records, key=lambda item: item.source_url)

    blocked_statuses = {"BLOCKED_ROBOTS", "BLOCKED_HTTP", "BLOCKED_CHALLENGE", "UNREACHABLE"}
    monitoring_statuses = {"STALE", "PARSE_FAILED"}

    action_required = [
        record
        for record in ordered_records
        if record.status in blocked_statuses and record.consecutive_failures >= CIRCUIT_OPEN_THRESHOLD
    ]
    degraded = [
        record
        for record in ordered_records
        if record.status in blocked_statuses and record.consecutive_failures < CIRCUIT_OPEN_THRESHOLD
    ]
    monitoring = [record for record in ordered_records if record.status in monitoring_statuses]
    healthy = [record for record in ordered_records if record.status == "OK"]

    changed_lines: list[str] = []
    for record in healthy:
        previous = previous_by_url.get(record.source_url)
        if previous is None:
            changed_lines.append(f"- {record.source_url}: NEW ({record.status})")
        elif previous.status != record.status:
            changed_lines.append(f"- {record.source_url}: {previous.status} -> {record.status}")

    lines = [
        "# Source Health Report",
        "",
        f"- Generated at: {timestamp.isoformat()}",
        f"- Total sources: {len(records)}",
        f"- OK: {counts.get('OK', 0)}",
        f"- STALE: {counts.get('STALE', 0)}",
        f"- PARSE_FAILED: {counts.get('PARSE_FAILED', 0)}",
        f"- BLOCKED_ROBOTS: {counts.get('BLOCKED_ROBOTS', 0)}",
        f"- BLOCKED_HTTP: {counts.get('BLOCKED_HTTP', 0)}",
        f"- BLOCKED_CHALLENGE: {counts.get('BLOCKED_CHALLENGE', 0)}",
        f"- UNREACHABLE: {counts.get('UNREACHABLE', 0)}",
        "",
        f"## Action Required ({len(action_required)})",
        "Circuit open or failing for 3+ consecutive checks — scraping this source is likely failing too.",
        "",
    ]
    lines.extend(_failure_table(action_required) if action_required else ["- None"])

    lines.extend(["", f"## Degraded ({len(degraded)})", "1-2 consecutive failures — watch, no action yet.", ""])
    lines.extend(_failure_table(degraded) if degraded else ["- None"])

    lines.extend(["", f"## Monitoring ({len(monitoring)})", "Reachable again after a stale gap, or otherwise worth a second look.", ""])
    if monitoring:
        for record in monitoring:
            lines.append(f"- {record.source_url} | status={record.status} | notes={record.notes}")
    else:
        lines.append("- None")

    lines.extend(["", f"## Healthy ({len(healthy)})"])
    if changed_lines:
        lines.append("Changed since previous run:")
        lines.extend(changed_lines)
    else:
        lines.append("- No status changes since the previous stored snapshot")

    return "\n".join(lines) + "\n"


def _failure_table(records: list[SourceHealthRecord]) -> list[str]:
    lines = []
    for record in records:
        lines.append(
            "- "
            f"{record.source_url} | status={record.status} | "
            f"consecutive_failures={record.consecutive_failures} | "
            f"failure_reason={record.failure_reason} | "
            f"suggested_action={record.suggested_action}"
        )
    return lines


def save_report(
    markdown_report: str,
    *,
    report_dir: str,
    generated_at: datetime | None = None,
) -> Path:
    timestamp = generated_at or datetime.now(timezone.utc)
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / f"source_health_{timestamp:%Y%m%d_%H%M%S}.md"
    output_path.write_text(markdown_report, encoding="utf-8")
    return output_path
