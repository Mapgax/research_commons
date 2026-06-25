from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from research_commons.source_health.checker import HTTPCheckResult, HomepageChecker
from research_commons.source_health.classifier import SourceHealthRecord, classify_result
from research_commons.source_health.report import (
    PreviousHealthRecord,
    build_weekly_report,
    insert_health_records,
    load_source_urls,
    update_circuit_breaker_state,
)


def test_checker_and_classifier_cover_working_blocked_empty_and_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path

        if path == "/robots.txt":
            if host == "blocked.example":
                return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")

        if host == "working.example":
            html = """
            <html><body>
            <h1>Working Example</h1>
            <p>Working Example provides market research, news coverage, and
            company updates for investors around the world every trading day.</p>
            <p>More detailed analysis, sector notes, and archive pages remain
            available on the homepage for navigation and discovery.</p>
            </body></html>
            """
            return httpx.Response(200, text=html)

        if host == "empty.example":
            return httpx.Response(200, text="<html><body>hi</body></html>")

        if host == "failing.example":
            raise httpx.ConnectError("DNS failure", request=request)

        raise AssertionError(f"Unexpected request: {request.url!s}")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, follow_redirects=True)

    with HomepageChecker(client=client, max_retries=0) as checker:
        records = [
            classify_result(checker.check_url("https://working.example")),
            classify_result(checker.check_url("https://blocked.example")),
            classify_result(checker.check_url("https://empty.example")),
            classify_result(checker.check_url("https://failing.example")),
        ]

    assert [record.status for record in records] == [
        "OK",
        "BLOCKED_ROBOTS",
        "PARSE_FAILED",
        "UNREACHABLE",
    ]


def test_insert_health_records_commits_rows(monkeypatch) -> None:
    connection = FakeConnection(fetch_sequences=[])

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr("research_commons.source_health.report.get_connection", fake_get_connection)

    record = SourceHealthRecord(
        source_url="https://working.example",
        status="OK",
        http_status=200,
        response_time=0.42,
        last_checked=datetime.now(timezone.utc),
        notes="meaningful content",
    )

    inserted = insert_health_records([record])

    assert inserted == 1
    assert connection.committed is True
    assert len(connection.cursor_obj.executemany_calls) == 1
    sql_text, params = connection.cursor_obj.executemany_calls[0]
    assert "INSERT INTO source_health" in sql_text
    assert params[0][0] == "https://working.example"
    assert params[0][1] == "OK"
    assert params[0][8] == 0
    assert params[0][9] is None


def test_load_source_urls_and_report_generation(monkeypatch) -> None:
    connection = FakeConnection(
        fetch_sequences=[
            [("active",), ("base_url",), ("url",)],
            [
                ("https://alpha.example",),
                ("ftp://ignored.example",),
                ("https://beta.example",),
            ],
        ]
    )

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr("research_commons.source_health.report.get_connection", fake_get_connection)

    urls = load_source_urls()
    assert urls == ["https://alpha.example", "https://beta.example"]

    now = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
    records = [
        SourceHealthRecord(
            source_url="https://alpha.example",
            status="BLOCKED_HTTP",
            http_status=403,
            response_time=0.9,
            last_checked=now,
            notes="HTTP 403 blocks homepage access",
            failure_reason="http_blocked",
            consecutive_failures=1,
            suggested_action="Site is actively blocking requests (401/403); consider rotating user agent or marking inactive.",
        ),
        SourceHealthRecord(
            source_url="https://beta.example",
            status="OK",
            http_status=200,
            response_time=0.3,
            last_checked=now,
            notes="meaningful content",
        ),
    ]
    previous = {
        "https://alpha.example": PreviousHealthRecord(
            source_url="https://alpha.example",
            status="OK",
            http_status=200,
            response_time=0.4,
            last_checked=now,
            notes="older snapshot",
        )
    }

    report = build_weekly_report(records, previous, generated_at=now)

    assert "- Total sources: 2" in report
    assert "- BLOCKED_HTTP: 1" in report
    assert "- UNREACHABLE: 0" in report
    assert "## Degraded (1)" in report
    assert "https://alpha.example" in report
    assert "https://beta.example: NEW (OK)" in report


def test_classify_result_flags_stale_after_long_gap() -> None:
    raw = HTTPCheckResult(
        source_url="https://working.example",
        checked_url="https://working.example",
        http_status=200,
        response_time=0.1,
        body_text="<html><body>" + ("market research news coverage " * 10) + "</body></html>",
        final_url="https://working.example",
    )
    now = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)

    fresh = classify_result(raw, checked_at=now, previous_last_ok_at=now - timedelta(days=1))
    assert fresh.status == "OK"

    stale = classify_result(raw, checked_at=now, previous_last_ok_at=now - timedelta(days=30))
    assert stale.status == "STALE"
    assert stale.consecutive_failures == 0

    never_seen = classify_result(raw, checked_at=now, previous_last_ok_at=None)
    assert never_seen.status == "OK"


def test_update_circuit_breaker_state_opens_and_clears(monkeypatch) -> None:
    connection = FakeConnection(
        fetch_sequences=[
            [("circuit_open",), ("last_ok_at",), ("url",)],
        ]
    )

    @contextmanager
    def fake_get_connection():
        yield connection

    monkeypatch.setattr("research_commons.source_health.report.get_connection", fake_get_connection)

    now = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
    records = [
        SourceHealthRecord(
            source_url="https://alpha.example",
            status="BLOCKED_HTTP",
            http_status=403,
            response_time=0.9,
            last_checked=now,
            notes="HTTP 403 blocks homepage access",
            consecutive_failures=3,
        ),
        SourceHealthRecord(
            source_url="https://beta.example",
            status="OK",
            http_status=200,
            response_time=0.3,
            last_checked=now,
            notes="meaningful content",
            consecutive_failures=0,
        ),
    ]

    update_circuit_breaker_state(records, checked_at=now)

    assert connection.committed is True
    sql_text, params = connection.cursor_obj.executed[-2]
    assert "UPDATE sources" in sql_text
    assert params["circuit_open"] is True
    assert params["source_url"] == "https://alpha.example"

    sql_text, params = connection.cursor_obj.executed[-1]
    assert params["circuit_open"] is False
    assert params["is_success"] is True
    assert params["source_url"] == "https://beta.example"


class FakeCursor:
    def __init__(self, fetch_sequences: list[list[tuple[object, ...]]]) -> None:
        self.fetch_sequences = list(fetch_sequences)
        self.executed: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql_text: str, params: object = None) -> None:
        self.executed.append((sql_text, params))

    def executemany(self, sql_text: str, seq_of_params: list[tuple[object, ...]]) -> None:
        self.executemany_calls.append((sql_text, list(seq_of_params)))

    def fetchall(self) -> list[tuple[object, ...]]:
        if not self.fetch_sequences:
            return []
        return self.fetch_sequences.pop(0)


class FakeConnection:
    def __init__(self, fetch_sequences: list[list[tuple[object, ...]]]) -> None:
        self.cursor_obj = FakeCursor(fetch_sequences)
        self.committed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True
