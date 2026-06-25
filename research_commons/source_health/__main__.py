"""Standalone weekly runner for homepage source-health checks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys

from research_commons.config import configure_logging, get_source_health_settings
from research_commons.source_health.checker import HomepageChecker
from research_commons.source_health.classifier import DEFAULT_STALE_THRESHOLD_DAYS, classify_result
from research_commons.source_health.emailer import send_report_email
from research_commons.source_health.report import (
    build_weekly_report,
    insert_health_records,
    load_previous_records,
    load_source_freshness_state,
    load_source_urls,
    save_report,
    update_circuit_breaker_state,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="Optional explicit homepage URL to check. Can be passed multiple times.",
    )
    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="Generate the report but do not send it even if Resend is configured.",
    )
    args = parser.parse_args(argv)

    settings = get_source_health_settings()
    configure_logging(settings.log_level)

    source_urls = args.urls or load_source_urls()
    if not source_urls:
        print("No source URLs found to check.", file=sys.stderr)
        return 1

    previous_by_url = load_previous_records(source_urls)
    freshness_by_url = load_source_freshness_state(source_urls)
    checked_at = datetime.now(timezone.utc)

    with HomepageChecker(
        timeout_sec=settings.source_health_timeout_sec,
        max_retries=settings.source_health_max_retries,
        user_agent=settings.source_health_user_agent,
    ) as checker:
        raw_results = checker.check_many(source_urls)

    records = []
    for raw in raw_results:
        freshness = freshness_by_url.get(raw.source_url)
        records.append(
            classify_result(
                raw,
                checked_at=checked_at,
                extra_keywords=settings.source_health_keywords,
                previous_consecutive_failures=(
                    previous_by_url[raw.source_url].consecutive_failures
                    if raw.source_url in previous_by_url
                    else 0
                ),
                previous_last_ok_at=freshness.last_ok_at if freshness else None,
                stale_threshold_days=(
                    freshness.stale_threshold_days if freshness else DEFAULT_STALE_THRESHOLD_DAYS
                ),
            )
        )

    insert_health_records(records)
    update_circuit_breaker_state(records, checked_at=checked_at)

    markdown_report = build_weekly_report(
        records,
        previous_by_url,
        generated_at=checked_at,
    )
    report_path = save_report(
        markdown_report,
        report_dir=settings.source_health_report_dir,
        generated_at=checked_at,
    )

    if (
        not args.skip_email
        and settings.resend_api_key
        and settings.resend_from
        and settings.email_to
    ):
        send_report_email(
            resend_api_key=settings.resend_api_key,
            resend_from=settings.resend_from,
            to_addrs=settings.email_to,
            subject=f"Source Health Report {checked_at:%Y-%m-%d}",
            markdown_body=markdown_report,
        )

    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
