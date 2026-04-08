"""Email delivery for source-health reports.

This reuses the same Resend-style environment variables already used in other
projects (`RESEND_API_KEY`, `RESEND_FROM`, `EMAIL_TO`), but the sending path is
fully separate from the existing reporting pipelines.

Security / external data flow:
    Sending a report transmits the report body to https://api.resend.com for
    delivery. That means the generated markdown summary leaves your machine.
"""

from __future__ import annotations

from html import escape
import logging

import httpx

logger = logging.getLogger(__name__)


def send_report_email(
    *,
    resend_api_key: str,
    resend_from: str,
    to_addrs: tuple[str, ...],
    subject: str,
    markdown_body: str,
) -> None:
    """Send the generated report via Resend's HTTP API."""

    if not to_addrs:
        raise ValueError("EMAIL_TO must contain at least one recipient")

    html_body = _markdown_to_html(markdown_body)
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": resend_from,
            "to": list(to_addrs),
            "subject": subject,
            "text": markdown_body,
            "html": html_body,
        },
        timeout=20.0,
    )
    response.raise_for_status()
    logger.info("Source-health report email sent to %s", ", ".join(to_addrs))


def _markdown_to_html(markdown_body: str) -> str:
    escaped = escape(markdown_body)
    return (
        "<html><body>"
        "<h2>Source Health Report</h2>"
        "<pre style=\"font-family: Menlo, Consolas, monospace; white-space: pre-wrap;\">"
        f"{escaped}"
        "</pre>"
        "</body></html>"
    )
