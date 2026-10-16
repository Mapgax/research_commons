from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

import pytest

from research_commons.db_news import reads, relevance
from research_commons.db_news.relevance import (
    BODY_EXCERPT_CHARS,
    HaikuRelevanceJudge,
    JevNotEnabledError,
    JevRelevanceJudge,
    JudgeUsage,
    RelevanceJudgment,
    RelevanceLink,
    judge_pending_links,
    rejudge_stale_links,
)
from research_commons.llm import LLMResult


def _link(article_id: int = 1, ticker: str = "AAPL", body: str = "Apple said...") -> RelevanceLink:
    return RelevanceLink(article_id, ticker, "Apple Inc.", "Apple beats estimates", body)


# ---------- fakes ------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql_text: str, params: object = None) -> None:
        self.conn.executed.append((sql_text, params))
        if sql_text == relevance._ATTEMPTS_TODAY_SQL:
            self._result: list[tuple[Any, ...]] = [(self.conn.attempted_today,)]
        else:
            self._result = list(self.conn.pending_rows)

    def executemany(self, sql_text: str, seq: list[object]) -> None:
        for params in seq:
            self.conn.executed.append((sql_text, params))

    def fetchone(self) -> tuple[Any, ...]:
        return self._result[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class FakeConnection:
    def __init__(self, *, pending_rows: list[tuple[Any, ...]], attempted_today: int = 0) -> None:
        self.pending_rows = pending_rows
        self.attempted_today = attempted_today
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def statements(self, needle: str) -> list[object]:
        return [params for sql, params in self.executed if needle in sql]


def _patch_db(monkeypatch, conn: FakeConnection) -> None:
    @contextmanager
    def fake_get_connection():
        yield conn

    monkeypatch.setattr(relevance, "get_connection", fake_get_connection)


class FakeJudge:
    version = "fake-v1"

    def __init__(self, p: dict[tuple[int, str], float | None]) -> None:
        self.p = p
        self.seen: list[RelevanceLink] = []
        self.usage = JudgeUsage()

    def judge(self, links: Sequence[RelevanceLink]) -> list[RelevanceJudgment]:
        self.seen.extend(links)
        return [
            RelevanceJudgment(
                link.article_id, link.ticker, self.p.get((link.article_id, link.ticker)),
                self.version, None if self.p.get((link.article_id, link.ticker)) is not None
                else "boom",
            )
            for link in links
        ]


# ---------- judge_pending_links ----------------------------------------------


def test_judge_pending_links_writes_judgments_and_marks_failures(monkeypatch) -> None:
    conn = FakeConnection(pending_rows=[
        (10, "AAPL", "Apple beats", "x" * 5000),
        (11, "MSFT", "Ten stocks", None),
    ])
    _patch_db(monkeypatch, conn)
    judge = FakeJudge({(10, "AAPL"): 0.92, (11, "MSFT"): None})

    stats = judge_pending_links(
        judge, company_names={"AAPL": "Apple Inc.", "MSFT": "Microsoft"}, daily_cap=100,
    )

    assert stats.selected == 2 and stats.judged == 1 and stats.failed == 1
    assert stats.cap_remaining == 98
    assert conn.statements("SET relevance_judged = %s") == [(0.92, "fake-v1", 10, "AAPL")]
    # A failure only stamps the attempt, so the prior stays in effect.
    assert conn.statements("SET relevance_judged_at = now()") == [(11, "MSFT")]
    assert judge.seen[0].company_name == "Apple Inc."
    assert len(judge.seen[0].body_excerpt) == BODY_EXCERPT_CHARS
    assert judge.seen[1].body_excerpt == ""
    assert conn.commits == 1


def test_judge_pending_links_selects_only_unjudged_named_tickers(monkeypatch) -> None:
    conn = FakeConnection(pending_rows=[])
    _patch_db(monkeypatch, conn)

    judge_pending_links(
        FakeJudge({}), company_names={"MSFT": "Microsoft", "AAPL": "Apple", "XX": " "},
        limit=25, daily_cap=1000,
    )

    sql_text, params = conn.executed[1]
    assert "ac.relevance_judged IS NULL" in sql_text
    assert "ac.relevance_judged_at < now() - interval '1 day'" in sql_text
    assert params == {"tickers": ["AAPL", "MSFT"], "limit": 25}


def test_judge_pending_links_respects_daily_cap(monkeypatch) -> None:
    conn = FakeConnection(pending_rows=[], attempted_today=95)
    _patch_db(monkeypatch, conn)

    judge_pending_links(FakeJudge({}), company_names={"AAPL": "Apple"}, limit=50, daily_cap=100)

    assert conn.executed[1][1]["limit"] == 5


def test_judge_pending_links_stops_at_cap_without_selecting(monkeypatch) -> None:
    conn = FakeConnection(pending_rows=[(1, "AAPL", "t", "b")], attempted_today=100)
    _patch_db(monkeypatch, conn)
    judge = FakeJudge({(1, "AAPL"): 0.9})

    stats = judge_pending_links(judge, company_names={"AAPL": "Apple"}, daily_cap=100)

    assert stats.selected == 0
    assert judge.seen == []
    assert len(conn.executed) == 1  # only the cap count


def test_judge_pending_links_since_bounds_the_scan(monkeypatch) -> None:
    from datetime import datetime, timezone

    conn = FakeConnection(pending_rows=[])
    _patch_db(monkeypatch, conn)
    cutoff = datetime(2026, 10, 13, tzinfo=timezone.utc)

    judge_pending_links(FakeJudge({}), company_names={"AAPL": "Apple"}, since=cutoff,
                        daily_cap=10)

    sql_text, params = conn.executed[1]
    assert "a.published_at >= %(since)s" in sql_text
    assert params["since"] == cutoff


def test_judge_pending_links_survives_a_raising_judge(monkeypatch) -> None:
    conn = FakeConnection(pending_rows=[(1, "AAPL", "t", "b"), (2, "AAPL", "t", "b")])
    _patch_db(monkeypatch, conn)

    class Exploding(FakeJudge):
        def judge(self, links):
            raise RuntimeError("judge bug")

    stats = judge_pending_links(Exploding({}), company_names={"AAPL": "Apple"}, daily_cap=10)

    assert stats.judged == 0 and stats.failed == 2 and stats.aborted is True
    assert conn.statements("SET relevance_judged = %s") == []


def test_judge_pending_links_aborts_after_a_fully_failed_chunk(monkeypatch) -> None:
    rows = [(i, "AAPL", "t", "b") for i in range(relevance.CHUNK_SIZE + 5)]
    conn = FakeConnection(pending_rows=rows)
    _patch_db(monkeypatch, conn)
    judge = FakeJudge({})  # every link fails

    stats = judge_pending_links(judge, company_names={"AAPL": "Apple"}, daily_cap=1000)

    assert stats.aborted is True
    assert len(judge.seen) == relevance.CHUNK_SIZE  # second chunk never sent


def test_judge_pending_links_ignores_judgments_for_links_it_did_not_ask_about(monkeypatch) -> None:
    conn = FakeConnection(pending_rows=[(1, "AAPL", "t", "b")])
    _patch_db(monkeypatch, conn)

    class Extra(FakeJudge):
        def judge(self, links):
            return [RelevanceJudgment(999, "EVIL", 1.0, "v"),
                    RelevanceJudgment(1, "AAPL", 0.4, "v")]

    judge_pending_links(Extra({}), company_names={"AAPL": "Apple"}, daily_cap=10)

    assert conn.statements("SET relevance_judged = %s") == [(0.4, "v", 1, "AAPL")]


# ---------- rejudge_stale_links / count_stale_links --------------------------


def test_rejudge_selects_other_versions_since_a_date(monkeypatch) -> None:
    from datetime import datetime, timezone

    conn = FakeConnection(pending_rows=[], attempted_today=40)
    _patch_db(monkeypatch, conn)
    cutoff = datetime(2026, 10, 1, tzinfo=timezone.utc)

    rejudge_stale_links(FakeJudge({}), company_names={"MSFT": "Microsoft", "AAPL": "Apple"},
                        since=cutoff, limit=500, daily_cap=100)

    sql_text, params = conn.executed[1]
    assert "split_part(ac.relevance_judge, '/', 1) <> %(version)s" in sql_text
    assert "ac.relevance_judge IS NOT NULL" in sql_text
    assert "ac.relevance_judged IS NULL" not in sql_text
    assert "ac.relevance_judged_at < now() - interval '1 day'" in sql_text
    assert "a.published_at >= %(since)s" in sql_text
    assert params == {"version": "fake-v1", "since": cutoff,
                      "tickers": ["AAPL", "MSFT"], "limit": 60}  # cap 100 - 40 today


def test_rejudge_overwrites_successes_and_keeps_old_judgment_on_failure(monkeypatch) -> None:
    from datetime import datetime, timezone

    conn = FakeConnection(pending_rows=[(10, "AAPL", "t", "b"), (11, "AAPL", "t", "b")])
    _patch_db(monkeypatch, conn)
    judge = FakeJudge({(10, "AAPL"): 0.3, (11, "AAPL"): None})

    stats = rejudge_stale_links(judge, company_names={"AAPL": "Apple"},
                                since=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                daily_cap=10)

    assert stats.judged == 1 and stats.failed == 1 and stats.cap_remaining == 8
    assert conn.statements("SET relevance_judged = %s") == [(0.3, "fake-v1", 10, "AAPL")]
    [(sql_text, params)] = [(q, p) for q, p in conn.executed
                            if "SET relevance_judged_at = now()" in q]
    # Stamps the attempt (so it waits a day and counts toward the cap) but
    # never nulls the old judgment: there is no SET of judged/judge here.
    assert params == (11, "AAPL")
    assert "relevance_judged =" not in sql_text and "relevance_judge =" not in sql_text
    assert "relevance_judged IS NULL" not in sql_text


def test_rejudge_respects_a_spent_cap(monkeypatch) -> None:
    from datetime import datetime, timezone

    conn = FakeConnection(pending_rows=[(1, "AAPL", "t", "b")], attempted_today=10)
    _patch_db(monkeypatch, conn)
    judge = FakeJudge({(1, "AAPL"): 0.9})

    stats = rejudge_stale_links(judge, company_names={"AAPL": "Apple"},
                                since=datetime(2026, 10, 1, tzinfo=timezone.utc), daily_cap=10)

    assert stats.selected == 0 and judge.seen == []


def test_count_stale_links_is_read_only(monkeypatch) -> None:
    from datetime import datetime, timezone

    conn = FakeConnection(pending_rows=[("haiku-v0/claude-haiku-4-5", 120),
                                        ("haiku-v0/gemini-2.5-flash", 3)])
    _patch_db(monkeypatch, conn)
    cutoff = datetime(2026, 10, 1, tzinfo=timezone.utc)

    counts = relevance.count_stale_links("haiku-v1", tickers=["MSFT", "AAPL", "AAPL"],
                                         since=cutoff)

    assert counts == {"haiku-v0/claude-haiku-4-5": 120, "haiku-v0/gemini-2.5-flash": 3}
    [(sql_text, params)] = conn.executed
    assert sql_text.lstrip().startswith("SELECT")
    assert params == {"version": "haiku-v1", "tickers": ["AAPL", "MSFT"], "since": cutoff}
    assert conn.commits == 0


# ---------- weekly_drift -----------------------------------------------------


def test_weekly_drift_computes_shares_and_reads_only(monkeypatch) -> None:
    from datetime import date

    conn = FakeConnection(pending_rows=[
        (date(2026, 10, 5), 1000, 900, 950, 50, [0.05, 0.1, 0.4, 0.8, 0.95], 90, 450,
         600, 330, "haiku-v1/claude-haiku-4-5"),
        (date(2026, 10, 12), 200, 0, 0, 0, None, 0, 0, 150, 40, None),
    ])
    _patch_db(monkeypatch, conn)

    w1, w2 = relevance.weekly_drift(weeks=4, threshold=0.6, tickers=["MSFT", "AAPL"])

    assert w1.judged_share == pytest.approx(0.9)
    assert w1.failure_rate == pytest.approx(50 / 950)
    assert w1.near_half_share == pytest.approx(0.1)
    assert w1.judged_pass_share == pytest.approx(0.5)
    assert w1.article_pass_share == pytest.approx(0.55)
    assert w1.p_median == pytest.approx(0.4)
    assert w1.judges == ("haiku-v1/claude-haiku-4-5",)
    # A week with nothing judged yet reports None, not 0, for judged-only shares.
    assert w2.failure_rate is None and w2.p_quantiles is None and w2.p_median is None
    assert w2.judged_pass_share is None and w2.judges == ()
    assert w2.article_pass_share == pytest.approx(40 / 150)

    [(sql_text, params)] = conn.executed
    assert "ac.ticker = ANY(%(tickers)s)" in sql_text
    assert params == {"weeks": 4, "threshold": 0.6, "tickers": ["AAPL", "MSFT"]}
    assert "UPDATE" not in sql_text.upper() and conn.commits == 0


def test_weekly_drift_without_tickers_scans_all_links(monkeypatch) -> None:
    conn = FakeConnection(pending_rows=[])
    _patch_db(monkeypatch, conn)

    assert relevance.weekly_drift(threshold=0.5) == []
    sql_text, params = conn.executed[0]
    assert "ANY(%(tickers)s)" not in sql_text and "tickers" not in params


# ---------- Jev judge --------------------------------------------------------


def test_jev_refuses_to_construct_without_opt_in() -> None:
    with pytest.raises(JevNotEnabledError):
        JevRelevanceJudge(opt_in=False, allow_fallback=False, transport=lambda body: {})


def test_jev_sends_only_the_allowed_fields() -> None:
    sent: list[dict[str, Any]] = []

    def transport(body: dict[str, Any]) -> dict[str, Any]:
        sent.append(body)
        return {"model": "jev-1.13.0",
                "answers": {"relevant": {"type": "noul", "noul": 0.83}},
                "usage": {"input_tokens": 300, "output_tokens": 20}}

    judge = JevRelevanceJudge(opt_in=True, allow_fallback=False, transport=transport)
    [judgment] = judge.judge([_link(body="y" * 4000)])

    assert judgment.p_relevant == pytest.approx(0.83)
    assert judgment.judge_version == "jev-v1/jev-1.13.0"
    body = sent[0]
    assert set(body) == {"state", "model", "questions"}
    assert body["state"] == {
        "article": {"title": "Apple beats estimates", "body_excerpt": "y" * BODY_EXCERPT_CHARS},
        "company": {"ticker": "AAPL", "name": "Apple Inc."},
    }
    assert list(body["questions"]) == ["relevant"]
    assert body["questions"]["relevant"]["type"] == "noul"
    assert judge.usage.input_tokens == 300 and judge.usage.calls == 1


def test_jev_failure_leaves_link_unjudged_without_fallback() -> None:
    fallback_calls: list[object] = []

    def down(body: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("typesafe down")

    judge = JevRelevanceJudge(
        opt_in=True, allow_fallback=False, transport=down,
        fallback_transport=lambda body: fallback_calls.append(body) or {},
    )
    [judgment] = judge.judge([_link()])

    assert judgment.p_relevant is None
    assert "typesafe down" in (judgment.error or "")
    assert fallback_calls == []


def test_jev_uses_fallback_only_when_allowed() -> None:
    def down(body: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("typesafe down")

    def fallback(body: dict[str, Any]) -> dict[str, Any]:
        return {"model": "jev-latest", "answers": {"relevant": {"noul": 0.2}}}

    judge = JevRelevanceJudge(
        opt_in=True, allow_fallback=True, transport=down, fallback_transport=fallback,
    )
    [judgment] = judge.judge([_link()])

    assert judgment.p_relevant == pytest.approx(0.2)
    assert judgment.judge_version.endswith("@fallback")


@pytest.mark.parametrize("answer", [{}, {"noul": 1.5}, {"noul": "0.7"}, {"noul": True}])
def test_jev_rejects_malformed_answers(answer: dict[str, Any]) -> None:
    judge = JevRelevanceJudge(
        opt_in=True, allow_fallback=False,
        transport=lambda body: {"answers": {"relevant": answer}},
    )
    [judgment] = judge.judge([_link()])
    assert judgment.p_relevant is None


# ---------- Haiku judge ------------------------------------------------------


class StubClient:
    """Returns ``content`` as the model's raw text (the judge asks for text and
    parses it itself). ``content`` may be a callable of the prompt."""

    def __init__(self, content: Any = None, exc: Exception | None = None) -> None:
        self.content = content
        self.exc = exc
        self.prompts: list[tuple[str, str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def generate(self, prompt: str, *, system: str = "", **kwargs: Any) -> LLMResult:
        self.prompts.append((prompt, system))
        self.kwargs.append(kwargs)
        if self.exc is not None:
            raise self.exc
        content = self.content(prompt) if callable(self.content) else self.content
        text = content if isinstance(content, str) else json.dumps(content)
        return LLMResult(text, text, "claude-haiku-4-5", "anthropic", 100, 10, 1, 250)


def test_haiku_matches_answers_by_echoed_id() -> None:
    client = StubClient([
        {"id": 2, "p_relevant": 0.1},
        {"id": 0, "p_relevant": 0.9},
        {"id": 0, "p_relevant": 0.0},   # duplicate: first wins
        {"id": 7, "p_relevant": 0.5},   # out of range: dropped
        # id 1 missing
    ])
    judge = HaikuRelevanceJudge(client, batch_size=10)  # type: ignore[arg-type]

    out = judge.judge([_link(1), _link(2, "MSFT"), _link(3, "NVDA")])

    assert [j.p_relevant for j in out] == [0.9, None, 0.1]
    assert out[1].error == "no answer with this id"
    assert out[0].judge_version == f"{relevance.HAIKU_JUDGE_VERSION}/claude-haiku-4-5"
    assert judge.usage.input_tokens == 100 and judge.usage.latencies_ms == [250]


def test_haiku_prompt_carries_only_the_allowed_fields() -> None:
    client = StubClient([{"id": 0, "p_relevant": 0.5}])
    HaikuRelevanceJudge(client).judge([_link(body="z" * 3000)])  # type: ignore[arg-type]

    prompt, system = client.prompts[0]
    assert prompt.startswith("[0] ")
    assert json.loads(prompt[4:]) == {
        "article": {"title": "Apple beats estimates", "body_excerpt": "z" * BODY_EXCERPT_CHARS},
        "company": {"ticker": "AAPL", "name": "Apple Inc."},
    }
    assert relevance.RELEVANCE_QUESTION in system
    assert client.kwargs[0]["response_format"] == "text"


def test_haiku_failure_returns_unjudged() -> None:
    judge = HaikuRelevanceJudge(StubClient(exc=RuntimeError("exhausted")))  # type: ignore[arg-type]

    out = judge.judge([_link(1), _link(2)])

    assert [j.p_relevant for j in out] == [None, None]
    assert judge.usage.failed_calls == 2 and judge.usage.calls == 2


def test_haiku_batches_and_resets_ids_per_batch() -> None:
    client = StubClient([{"id": 0, "p_relevant": 0.6}, {"id": 1, "p_relevant": 0.4}])
    judge = HaikuRelevanceJudge(client, batch_size=2, max_workers=1)  # type: ignore[arg-type]

    out = judge.judge([_link(1), _link(2), _link(3)])

    assert len(client.prompts) == 2
    assert [j.p_relevant for j in out] == [0.6, 0.4, 0.6]


# Regression, haiku-v1 (2026-10-16 event study): in batches of 10 a clearly
# relevant item was scored like its irrelevant neighbours (ZS "shares tumble
# 31%": 0.95 alone, 0.02 in its batch). The fake model below anchors the same
# way — every item gets the batch's majority verdict — so a link's score only
# stays its own if it is judged alone.


def _anchoring_model(prompt: str) -> list[dict[str, Any]]:
    items = [json.loads(line.split("] ", 1)[1]) for line in prompt.splitlines()]
    own = [0.95 if "Zscaler" in item["article"]["title"] else 0.02 for item in items]
    majority = sorted(own)[len(own) // 2]
    return [{"id": i, "ticker": item["company"]["ticker"], "p_relevant": majority}
            for i, item in enumerate(items)]


def _zs_batch() -> list[RelevanceLink]:
    noise = [RelevanceLink(i, "META", "Meta Platforms", f"Stocks to watch #{i}", "...")
             for i in range(9)]
    zs = RelevanceLink(99, "ZS", "Zscaler", "Zscaler shares tumble 31% despite beat", "...")
    return noise[:8] + [zs] + noise[8:]


def test_haiku_score_does_not_depend_on_batch_neighbours() -> None:
    client = StubClient(_anchoring_model)
    out = HaikuRelevanceJudge(client).judge(_zs_batch())  # type: ignore[arg-type]

    assert all(prompt.count("\n") == 0 for prompt, _ in client.prompts)  # one link per call
    assert len(client.prompts) == 10
    by_ticker = {j.ticker: j.p_relevant for j in out if j.article_id in (0, 99)}
    assert by_ticker == {"ZS": 0.95, "META": 0.02}
    # Results keep input order even though calls run concurrently.
    assert [j.article_id for j in out] == [link.article_id for link in _zs_batch()]


def test_haiku_batching_reproduces_the_anchoring_bug() -> None:
    # Documents why batch_size stays 1: batched, ZS takes its neighbours' score.
    out = HaikuRelevanceJudge(StubClient(_anchoring_model), batch_size=10).judge(  # type: ignore[arg-type]
        _zs_batch())
    assert next(j.p_relevant for j in out if j.ticker == "ZS") == 0.02


@pytest.mark.parametrize("text", [
    '```json\n[{"id": 0, "p_relevant": 0.15}]\n```\n\nThe article is primarily about Meta...',
    'Here is my answer:\n[{"id": 0, "p_relevant": 0.15}]',
    '{"id": 0, "p_relevant": 0.15}',
])
def test_haiku_parses_answers_wrapped_in_fences_or_prose(text: str) -> None:
    # haiku-v1 lost ~half of single-item calls: Haiku appended an explanation
    # after the fenced array and the strict client parser raised.
    [judgment] = HaikuRelevanceJudge(StubClient(text)).judge([_link()])  # type: ignore[arg-type]
    assert judgment.p_relevant == pytest.approx(0.15)


@pytest.mark.parametrize("text", ["", "I cannot judge this.", "[not json"])
def test_haiku_unparseable_response_leaves_link_unjudged(text: str) -> None:
    [judgment] = HaikuRelevanceJudge(StubClient(text)).judge([_link()])  # type: ignore[arg-type]
    assert judgment.p_relevant is None and judgment.error == "no JSON array in response"


def test_haiku_rejects_an_answer_for_the_wrong_ticker() -> None:
    client = StubClient([{"id": 0, "ticker": "MSFT", "p_relevant": 0.9}])
    [judgment] = HaikuRelevanceJudge(client).judge([_link()])  # type: ignore[arg-type]
    assert judgment.p_relevant is None and "MSFT" in (judgment.error or "")

    client = StubClient([{"id": 0, "ticker": "aapl ", "p_relevant": 0.9}])
    [judgment] = HaikuRelevanceJudge(client).judge([_link()])  # type: ignore[arg-type]
    assert judgment.p_relevant == 0.9


def test_haiku_output_budget_leaves_room_for_the_gemini_fallback() -> None:
    # 2.5-flash spends max_output_tokens on thinking; v1's 96 returned empty text.
    client = StubClient([{"id": 0, "p_relevant": 0.5}])
    HaikuRelevanceJudge(client).judge([_link()])  # type: ignore[arg-type]
    assert client.kwargs[0]["max_output_tokens"] >= 512


# ---------- configuration / reads --------------------------------------------


def test_make_relevance_judge_defaults_to_none() -> None:
    from dataclasses import replace

    from research_commons.config import Settings

    base = Settings(market_database_url="", news_database_url="x",
                    anthropic_api_key=None, gemini_api_key=None)
    assert relevance.make_relevance_judge(base) is None
    with pytest.raises(JevNotEnabledError):
        relevance.make_relevance_judge(replace(base, relevance_judge="jev",
                                               typesafe_api_key="k"))
    with pytest.raises(ValueError):
        relevance.make_relevance_judge(replace(base, relevance_judge="gpt"))


def test_effective_relevance_prefers_judged() -> None:
    assert reads.effective_relevance(0.1, 1.0) == 0.1
    assert reads.effective_relevance(0.0, 1.0) == 0.0
    assert reads.effective_relevance(None, 0.6) == 0.6
    assert reads.effective_relevance(None, None) is None
    assert reads.effective_relevance_sql("x") == "COALESCE(x.relevance_judged, x.relevance)"
