"""Standalone weekly runner for homepage source-health checks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys

from research_commons.config import configure_logging, get_source_health_settings
from research_commons.source_health.checker import HomepageChecker
from research_commons.source_health.classifier import classify_result
from research_commons.source_health.emailer import send_report_email
from research_commons.source_health.report import (
    build_weekly_report,
    insert_health_records,
    load_previous_records,
    load_source_urls,
    save_report,
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
    checked_at = datetime.now(timezone.utc)

    with HomepageChecker(
        timeout_sec=settings.source_health_timeout_sec,
        max_retries=settings.source_health_max_retries,
        user_agent=settings.source_health_user_agent,
    ) as checker:
        raw_results = checker.check_many(source_urls)

    records = [
        classify_result(
            raw,
            checked_at=checked_at,
            extra_keywords=settings.source_health_keywords,
        )
        for raw in raw_results
    ]

    insert_health_records(records)

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
