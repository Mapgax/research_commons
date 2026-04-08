"""Database IO and weekly report generation for source-health snapshots."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import logging

from research_commons.db_news import get_connection
from research_commons.source_health.classifier import SourceHealthRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreviousHealthRecord:
    source_url: str
    status: str
    http_status: int | None
    response_time: float | None
    last_checked: datetime
    notes: str


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
                    notes
                FROM source_health
                WHERE source_url = ANY(%s)
                ORDER BY source_url, last_checked DESC, id DESC
                """,
                (ordered_urls,),
            )
            rows = cur.fetchall()

    previous: dict[str, PreviousHealthRecord] = {}
    for source_url, status, http_status, response_time, last_checked, notes in rows:
        previous[source_url] = PreviousHealthRecord(
            source_url=source_url,
            status=status,
            http_status=http_status,
            response_time=response_time,
            last_checked=last_checked,
            notes=notes or "",
        )
    return previous


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
                    notes
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        record.source_url,
                        record.status,
                        record.http_status,
                        record.response_time,
                        record.last_checked,
                        record.notes,
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
    timestamp = generated_at or datetime.now(timezone.utc)
    counts = Counter(record.status for record in records)
    blocked_or_error = [
        record for record in records if record.status in {"BLOCKED", "ERROR"}
    ]

    changed_lines: list[str] = []
    for record in sorted(records, key=lambda item: item.source_url):
        previous = previous_by_url.get(record.source_url)
        if previous is None:
            changed_lines.append(f"- {record.source_url}: NEW ({record.status})")
            continue
        if previous.status != record.status:
            changed_lines.append(
                f"- {record.source_url}: {previous.status} -> {record.status}"
            )

    lines = [
        "# Source Health Report",
        "",
        f"- Generated at: {timestamp.isoformat()}",
        f"- Total sources: {len(records)}",
        f"- WORKING: {counts.get('WORKING', 0)}",
        f"- ACCESSIBLE_NO_CONTENT: {counts.get('ACCESSIBLE_NO_CONTENT', 0)}",
        f"- BLOCKED: {counts.get('BLOCKED', 0)}",
        f"- ERROR: {counts.get('ERROR', 0)}",
        "",
        "## Blocked And Error Sources",
    ]

    if blocked_or_error:
        for record in blocked_or_error:
            lines.append(
                "- "
                f"{record.source_url} | status={record.status} | "
                f"http_status={record.http_status} | notes={record.notes}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## Changes Vs Previous Run"])
    if changed_lines:
        lines.extend(changed_lines)
    else:
        lines.append("- No status changes since the previous stored snapshot")

    return "\n".join(lines) + "\n"


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
