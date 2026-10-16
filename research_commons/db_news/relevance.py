"""Judged (article, ticker) relevance — the relevance gate.

Spec: ``docs/spec-jev-relevance-gate.md``.

``article_companies.relevance`` is a string-match prior. This module asks a
cheap judge "is this article materially about this company?" per link and
stores the answer next to the prior in ``relevance_judged``. Consumers read
the two through ``reads.effective_relevance_sql`` / ``load_article_links``,
never restating the COALESCE themselves.

Frozen API surface:

    RelevanceLink, RelevanceJudgment, JudgeUsage, JudgeRunStats
    RelevanceJudge                       protocol: .version, .usage, .judge(links)
    HaikuRelevanceJudge(client=None, *, batch_size=1, max_workers=4, system=...)
    JevRelevanceJudge(*, api_key=None, opt_in=None, allow_fallback=None,
                      transport=None, fallback_transport=None, max_workers=8)
    make_relevance_judge(settings=None) -> RelevanceJudge | None
    judge_pending_links(judge, *, company_names, limit=None, since=None,
                        daily_cap=None) -> JudgeRunStats
    rejudge_stale_links(judge, *, company_names, since, limit=None,
                        daily_cap=None) -> JudgeRunStats
    count_stale_links(judge_version, *, tickers, since) -> dict[str, int]
    WeeklyDrift, weekly_drift(*, weeks=8, threshold=None, tickers=None)

Privacy: the payload a judge sends is built here from :class:`RelevanceLink`
alone — title, a body excerpt capped at :data:`BODY_EXCERPT_CHARS`, ticker and
company name. Callers cannot add fields (no scope_status, PM/email flags,
holdings). The Jev judge additionally refuses to construct without the
``RELEVANCE_JEV_OPT_IN`` sign-off flag, and never falls back to a second
processor unless ``RELEVANCE_JEV_ALLOW_FALLBACK`` is set *and* a fallback
transport is passed explicitly — none ships with this library.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol

from research_commons.config import Settings, get_settings
from research_commons.db_news.connection import get_connection

if TYPE_CHECKING:
    from research_commons.llm import LLMClient

logger = logging.getLogger(__name__)

BODY_EXCERPT_CHARS = 1000

# One definition of "relevant", shared by both judges so the eval compares
# models rather than wordings. Bump the judge versions below when it changes.
RELEVANCE_QUESTION = (
    "Is the article in `article` primarily about `company.name` (ticker "
    "`company.ticker`), or does it report an event, result, decision or "
    "development specific to that company? Answer no if the company is only "
    "listed, quoted as an example, mentioned in passing, or if the name matches "
    "a different entity."
)

HAIKU_JUDGE_VERSION = "haiku-v2"   # v2 2026-10-16: one link per call, see the runbook
JEV_JUDGE_VERSION = "jev-v1"

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"

# judge_pending_links tuning.
RETRY_AFTER = "1 day"          # a failed link is retried after this long
CHUNK_SIZE = 50                # links per judge call batch / DB write
FAILURE_WARN_RATE = 0.10       # WARNING above this share of failed links


# ---------- data shapes ------------------------------------------------------


@dataclass(frozen=True)
class RelevanceLink:
    """Everything a judge may see about one (article, ticker) link."""

    article_id: int
    ticker: str
    company_name: str
    title: str
    body_excerpt: str


@dataclass(frozen=True)
class RelevanceJudgment:
    """``p_relevant`` is a probability, not an intensity. ``None`` = unjudged."""

    article_id: int
    ticker: str
    p_relevant: float | None
    judge_version: str
    error: str | None = None


@dataclass
class JudgeUsage:
    """Running cost/latency counters, read by the offline eval."""

    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latencies_ms: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class JudgeRunStats:
    selected: int
    judged: int
    failed: int
    cap_remaining: int
    aborted: bool = False


class RelevanceJudge(Protocol):
    version: str
    usage: JudgeUsage

    def judge(self, links: Sequence[RelevanceLink]) -> list[RelevanceJudgment]:
        """Return one judgment per link. Never raises for a per-link failure."""
        ...


def link_state(link: RelevanceLink) -> dict[str, Any]:
    """The only payload any judge sends for a link."""
    return {
        "article": {
            "title": link.title,
            "body_excerpt": link.body_excerpt[:BODY_EXCERPT_CHARS],
        },
        "company": {"ticker": link.ticker, "name": link.company_name},
    }


def _valid_probability(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    p = float(value)
    if math.isnan(p) or not 0.0 <= p <= 1.0:
        return None
    return p


def _failed(link: RelevanceLink, version: str, error: str) -> RelevanceJudgment:
    return RelevanceJudgment(link.article_id, link.ticker, None, version, error)


# ---------- Haiku (existing LLMClient) ---------------------------------------


_HAIKU_SYSTEM = (
    "You judge whether news articles are materially about a given listed company.\n"
    "Each item below is prefixed with an integer id in brackets and holds an "
    "`article` and a `company`. Judge every item on its own: the other items have "
    "no bearing on it, and most batches mix relevant and irrelevant items. For each "
    "item, first note which company or entity its article is mainly about, then "
    "answer this question for that item's own `company`:\n"
    f"{RELEVANCE_QUESTION}\n"
    "Give the probability that the answer is yes, as a float from 0 to 1.\n"
    'Return ONLY a JSON array with one object per item: {"id": <the integer id, '
    'copied back exactly>, "ticker": <the item\'s company.ticker, copied back '
    'exactly>, "p_relevant": <float 0..1>}. No markdown, no code fences and no '
    "explanation before or after the array. Every item must get exactly one object "
    "with its matching id."
)

# Output budget per call. Generous on purpose: Anthropic stops at end_turn, and
# the Gemini fallback (2.5-flash) spends part of max_output_tokens on thinking,
# so a tight budget returns empty text there.
_HAIKU_BASE_TOKENS = 512
_HAIKU_TOKENS_PER_ITEM = 48


def _parse_json_answers(text: str) -> Any:
    """First JSON array or object in ``text``; ``None`` if there is none.

    Tolerates code fences and prose before or after it — Haiku tends to
    append an explanation to single-item answers, which the shared client's
    strict parser rejects.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                value, _ = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            return value
    return None


class HaikuRelevanceJudge:
    """Company-aware relevance prompt through the shared :class:`LLMClient`.

    One link per call by default, so a link's score cannot depend on the
    links judged next to it: in batches of 10, haiku-v1 scored clearly
    relevant items near 0 when their neighbours were irrelevant (ZS "shares
    tumble 31%": 0.95 alone, 0.02 in its batch). ``batch_size > 1`` still
    works — answers are matched by the echoed id, never by position, and an
    echoed ticker that doesn't match the item fails it — but is for
    experiments. Calls run on ``max_workers`` threads; ``LLMClient`` is
    thread-safe.
    """

    version = HAIKU_JUDGE_VERSION

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        batch_size: int = 1,
        max_workers: int = 4,
        system: str = _HAIKU_SYSTEM,
    ) -> None:
        if client is None:
            from research_commons.llm import LLMClient

            client = LLMClient()
        self._client = client
        self._batch_size = max(batch_size, 1)
        self._max_workers = max(max_workers, 1)
        self._system = system
        self._lock = threading.Lock()
        self.usage = JudgeUsage()

    def judge(self, links: Sequence[RelevanceLink]) -> list[RelevanceJudgment]:
        batches = [links[i:i + self._batch_size]
                   for i in range(0, len(links), self._batch_size)]
        if not batches:
            return []
        workers = min(self._max_workers, len(batches))
        if workers == 1:
            results = [self._judge_batch(b) for b in batches]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(self._judge_batch, batches))
        return [j for batch in results for j in batch]

    def _judge_batch(self, batch: Sequence[RelevanceLink]) -> list[RelevanceJudgment]:
        items = [
            f"[{idx}] {json.dumps(link_state(link), ensure_ascii=False)}"
            for idx, link in enumerate(batch)
        ]
        try:
            result = self._client.generate(
                "\n".join(items),
                system=self._system,
                response_format="text",
                max_output_tokens=_HAIKU_BASE_TOKENS + _HAIKU_TOKENS_PER_ITEM * len(batch),
            )
        except Exception as exc:
            with self._lock:
                self.usage.calls += 1
                self.usage.failed_calls += 1
            logger.warning("Haiku relevance judge: batch of %d failed: %s", len(batch), exc)
            return [_failed(link, self.version, f"llm error: {exc}") for link in batch]

        with self._lock:
            self.usage.calls += 1
            self.usage.input_tokens += result.input_tokens
            self.usage.output_tokens += result.output_tokens
            self.usage.latencies_ms.append(result.latency_ms)
        version = f"{self.version}/{result.model_used}"

        raw = result.content if isinstance(result.content, str) else result.raw_text
        answers = _parse_json_answers(raw or "")
        if isinstance(answers, dict):
            answers = [answers]
        if not isinstance(answers, list):
            return [_failed(link, version, "no JSON array in response") for link in batch]

        by_id: dict[int, float | str] = {}
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            answer_id = answer.get("id")
            if isinstance(answer_id, bool) or not isinstance(answer_id, int):
                continue
            if not 0 <= answer_id < len(batch) or answer_id in by_id:
                continue  # out of range, or a duplicate: keep the first
            echoed = answer.get("ticker")
            if echoed is not None and str(echoed).strip().upper() != batch[answer_id].ticker.upper():
                by_id[answer_id] = f"answer echoes ticker {echoed!r}"
                continue
            p = _valid_probability(answer.get("p_relevant"))
            by_id[answer_id] = "p_relevant missing or outside 0..1" if p is None else p

        out: list[RelevanceJudgment] = []
        for idx, link in enumerate(batch):
            value = by_id.get(idx, "no answer with this id")
            if isinstance(value, str):
                out.append(_failed(link, version, value))
            else:
                out.append(RelevanceJudgment(link.article_id, link.ticker, value, version))
        return out


# ---------- Jev (TypeSafe System One, noul) ----------------------------------


Transport = Callable[[dict[str, Any]], dict[str, Any]]


class JevNotEnabledError(RuntimeError):
    """Raised when the Jev judge is built without the privacy sign-off flag."""


def _typesafe_transport(api_key: str, timeout_sec: float) -> Transport:
    import httpx

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def post(body: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(TYPESAFE_URL, json=body, headers=headers, timeout=timeout_sec)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    return post


class JevRelevanceJudge:
    """One ``noul`` question per link, links judged in parallel calls.

    TypeSafe only. A failed call leaves the link unjudged (the prior stays in
    effect); it is routed to ``fallback_transport`` only when
    ``allow_fallback`` is true *and* such a transport is passed.
    """

    version = JEV_JUDGE_VERSION

    def __init__(
        self,
        *,
        api_key: str | None = None,
        opt_in: bool | None = None,
        allow_fallback: bool | None = None,
        transport: Transport | None = None,
        fallback_transport: Transport | None = None,
        max_workers: int = 8,
        timeout_sec: float = 30.0,
    ) -> None:
        if opt_in is None or allow_fallback is None or (api_key is None and transport is None):
            settings = get_settings()
            opt_in = settings.relevance_jev_opt_in if opt_in is None else opt_in
            if allow_fallback is None:
                allow_fallback = settings.relevance_jev_allow_fallback
            api_key = api_key or settings.typesafe_api_key
        if not opt_in:
            raise JevNotEnabledError(
                "Jev relevance judge is disabled: sending article excerpts to TypeSafe "
                "needs the Phase 0 privacy sign-off (RELEVANCE_JEV_OPT_IN=true). "
                "See docs/spec-jev-relevance-gate.md."
            )
        if transport is None:
            if not api_key:
                raise RuntimeError("TYPESAFE_API_KEY is not set")
            transport = _typesafe_transport(api_key, timeout_sec)
        if allow_fallback and fallback_transport is None:
            logger.warning(
                "RELEVANCE_JEV_ALLOW_FALLBACK is set but no fallback transport was "
                "passed; failed Jev calls stay unjudged."
            )
        self._transport = transport
        self._fallback = fallback_transport if allow_fallback else None
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self.usage = JudgeUsage()

    @staticmethod
    def request_body(link: RelevanceLink) -> dict[str, Any]:
        return {
            "state": link_state(link),
            "model": JEV_MODEL,
            "questions": {
                "relevant": {
                    "type": "noul",
                    "instructions": RELEVANCE_QUESTION,
                    "criteria": {
                        "true": "The article is about this company or reports news "
                                "specific to it.",
                        "false": "The company is only listed, used as an example, "
                                 "mentioned in passing, or the name refers to a "
                                 "different entity.",
                    },
                },
            },
        }

    def judge(self, links: Sequence[RelevanceLink]) -> list[RelevanceJudgment]:
        if not links:
            return []
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(links))) as pool:
            return list(pool.map(self._judge_one, links))

    def _judge_one(self, link: RelevanceLink) -> RelevanceJudgment:
        body = self.request_body(link)
        try:
            return self._call(self._transport, body, link, suffix="")
        except Exception as exc:
            if self._fallback is None:
                return _failed(link, self.version, f"typesafe error: {exc}")
            logger.warning("Jev relevance judge: TypeSafe failed (%s); using fallback", exc)
            try:
                return self._call(self._fallback, body, link, suffix="@fallback")
            except Exception as fallback_exc:
                return _failed(link, self.version, f"fallback error: {fallback_exc}")

    def _call(
        self, transport: Transport, body: dict[str, Any], link: RelevanceLink, *, suffix: str,
    ) -> RelevanceJudgment:
        t0 = time.monotonic_ns()
        try:
            response = transport(body)
        except Exception:
            with self._lock:
                self.usage.calls += 1
                self.usage.failed_calls += 1
            raise
        latency_ms = int((time.monotonic_ns() - t0) / 1_000_000)
        usage = response.get("usage") or {}
        with self._lock:
            self.usage.calls += 1
            self.usage.input_tokens += int(usage.get("input_tokens") or 0)
            self.usage.output_tokens += int(usage.get("output_tokens") or 0)
            self.usage.latencies_ms.append(latency_ms)

        version = f"{self.version}/{response.get('model') or JEV_MODEL}{suffix}"
        answer = (response.get("answers") or {}).get("relevant") or {}
        p = _valid_probability(answer.get("noul"))
        if p is None:
            return _failed(link, version, "no valid noul in response")
        return RelevanceJudgment(link.article_id, link.ticker, p, version)


# ---------- configuration ----------------------------------------------------


def make_relevance_judge(settings: Settings | None = None) -> RelevanceJudge | None:
    """Build the judge named by ``RELEVANCE_JUDGE`` (``none`` -> ``None``)."""
    s = settings or get_settings()
    choice = s.relevance_judge
    if choice == "none":
        return None
    if choice == "haiku":
        return HaikuRelevanceJudge()
    if choice == "jev":
        return JevRelevanceJudge(
            api_key=s.typesafe_api_key,
            opt_in=s.relevance_jev_opt_in,
            allow_fallback=s.relevance_jev_allow_fallback,
        )
    raise ValueError(f"RELEVANCE_JUDGE must be none, haiku or jev; got {choice!r}")


# ---------- production path --------------------------------------------------


_ATTEMPTS_TODAY_SQL = """
    SELECT count(*)
    FROM article_companies
    WHERE relevance_judged_at >= date_trunc('day', now(), 'UTC')
"""

_PENDING_SQL = f"""
    SELECT ac.article_id, ac.ticker, a.title, left(a.body, {BODY_EXCERPT_CHARS})
    FROM article_companies ac
    JOIN articles a ON a.id = ac.article_id
    WHERE ac.relevance_judged IS NULL
      AND (ac.relevance_judged_at IS NULL
           OR ac.relevance_judged_at < now() - interval '{RETRY_AFTER}')
      AND ac.ticker = ANY(%(tickers)s)
      {{since_clause}}
    ORDER BY a.published_at DESC NULLS LAST, ac.article_id DESC
    LIMIT %(limit)s
"""

_WRITE_JUDGED_SQL = """
    UPDATE article_companies
    SET relevance_judged = %s, relevance_judge = %s, relevance_judged_at = now()
    WHERE article_id = %s AND ticker = %s
"""

# A failure touches only the attempt timestamp: judged and judge stay NULL,
# so relevance_judge IS NOT NULL <=> relevance_judged IS NOT NULL.
_MARK_FAILED_SQL = """
    UPDATE article_companies
    SET relevance_judged_at = now()
    WHERE article_id = %s AND ticker = %s AND relevance_judged IS NULL
"""


def judge_pending_links(
    judge: RelevanceJudge,
    *,
    company_names: Mapping[str, str],
    limit: int | None = None,
    since: datetime | None = None,
    daily_cap: int | None = None,
) -> JudgeRunStats:
    """Judge links that have no judgment yet and write the results.

    ``company_names`` maps ticker -> company name (news_db does not store
    names); links for tickers missing from it are not selected. ``since``
    bounds the scan by ``articles.published_at`` — pass it on the daily path
    so the pending query does not sort the whole unjudged history.

    Each attempt counts toward ``daily_cap`` (default ``RELEVANCE_DAILY_CAP``)
    whether it succeeds or not. A failed link stays unjudged and is retried
    after :data:`RETRY_AFTER`; a judge outage degrades to the prior and never
    raises. A chunk in which every link fails stops the run.
    """
    params: dict[str, Any] = {}
    since_clause = ""
    if since is not None:
        since_clause = "AND a.published_at >= %(since)s"
        params["since"] = since
    return _judge_and_write(
        judge, "judge_pending_links", company_names=company_names, limit=limit,
        daily_cap=daily_cap, select_sql=_PENDING_SQL.format(since_clause=since_clause),
        params=params, mark_failed_sql=_MARK_FAILED_SQL,
    )


_STALE_WHERE = f"""
    ac.relevance_judge IS NOT NULL
      AND split_part(ac.relevance_judge, '/', 1) <> %(version)s
      AND ac.relevance_judged_at < now() - interval '{RETRY_AFTER}'
      AND ac.ticker = ANY(%(tickers)s)
      AND a.published_at >= %(since)s
"""

_STALE_SQL = f"""
    SELECT ac.article_id, ac.ticker, a.title, left(a.body, {BODY_EXCERPT_CHARS})
    FROM article_companies ac
    JOIN articles a ON a.id = ac.article_id
    WHERE {_STALE_WHERE}
    ORDER BY a.published_at DESC NULLS LAST, ac.article_id DESC
    LIMIT %(limit)s
"""

_COUNT_STALE_SQL = f"""
    SELECT ac.relevance_judge, count(*)
    FROM article_companies ac
    JOIN articles a ON a.id = ac.article_id
    WHERE {_STALE_WHERE}
    GROUP BY ac.relevance_judge
    ORDER BY count(*) DESC
"""

# A failed re-judge keeps the old judgment and stamps the attempt, so the
# link waits RETRY_AFTER and the attempt counts toward the daily cap.
_MARK_REJUDGE_FAILED_SQL = """
    UPDATE article_companies
    SET relevance_judged_at = now()
    WHERE article_id = %s AND ticker = %s
"""


def rejudge_stale_links(
    judge: RelevanceJudge,
    *,
    company_names: Mapping[str, str],
    since: datetime,
    limit: int | None = None,
    daily_cap: int | None = None,
) -> JudgeRunStats:
    """Re-judge links whose judgment came from a different judge version.

    Opt-in companion to :func:`judge_pending_links`, which only ever picks
    unjudged links — so bumping :data:`HAIKU_JUDGE_VERSION` after a prompt
    change would otherwise leave every existing judgment on the old version.
    A link is stale when the part of ``relevance_judge`` before the first
    ``/`` differs from ``judge.version``; the model suffix is ignored, so a
    model swap needs a version bump to count.

    Bounded by ``since`` (``articles.published_at``, required) and by the same
    daily cap as the pending path, which it shares. Links attempted within
    :data:`RETRY_AFTER` are skipped. A failed re-judge keeps the old judgment.
    """
    return _judge_and_write(
        judge, "rejudge_stale_links", company_names=company_names, limit=limit,
        daily_cap=daily_cap, select_sql=_STALE_SQL,
        params={"version": judge.version, "since": since},
        mark_failed_sql=_MARK_REJUDGE_FAILED_SQL,
    )


def count_stale_links(
    judge_version: str, *, tickers: Sequence[str], since: datetime,
) -> dict[str, int]:
    """Read-only: stale links per stored ``relevance_judge``, as
    :func:`rejudge_stale_links` would select them (before the cap)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(_COUNT_STALE_SQL, {
            "version": judge_version, "tickers": sorted(set(tickers)), "since": since,
        })
        return {str(version): int(n) for version, n in cur.fetchall()}


def _judge_and_write(
    judge: RelevanceJudge,
    caller: str,
    *,
    company_names: Mapping[str, str],
    limit: int | None,
    daily_cap: int | None,
    select_sql: str,
    params: dict[str, Any],
    mark_failed_sql: str,
) -> JudgeRunStats:
    """Select up to the cap with ``select_sql``, judge in chunks, write back."""
    names = {t: n.strip() for t, n in company_names.items() if t and n and n.strip()}
    if not names:
        logger.warning("%s: no company names given — nothing to judge", caller)
        return JudgeRunStats(0, 0, 0, 0)
    cap = get_settings().relevance_daily_cap if daily_cap is None else daily_cap
    params = {**params, "tickers": sorted(names)}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_ATTEMPTS_TODAY_SQL)
            remaining = max(cap - int(cur.fetchone()[0]), 0)
            if remaining == 0:
                logger.warning("%s: daily cap of %d judge attempts reached", caller, cap)
                return JudgeRunStats(0, 0, 0, 0)
            params["limit"] = remaining if limit is None else min(limit, remaining)
            cur.execute(select_sql, params)
            rows = cur.fetchall()
    links = [
        RelevanceLink(
            article_id=int(article_id),
            ticker=ticker,
            company_name=names[ticker],
            title=title or "",
            body_excerpt=(body or "")[:BODY_EXCERPT_CHARS],
        )
        for article_id, ticker, title, body in rows
    ]

    judged = failed = 0
    aborted = False
    sample_errors: list[str] = []
    for i in range(0, len(links), CHUNK_SIZE):
        chunk = links[i:i + CHUNK_SIZE]
        try:
            judgments = judge.judge(chunk)
        except Exception as exc:  # a judge bug must not fail collection
            logger.warning("%s: judge raised: %s", caller, exc)
            judgments = []

        by_key: dict[tuple[int, str], RelevanceJudgment] = {}
        for j in judgments:
            by_key.setdefault((j.article_id, j.ticker), j)

        ok_rows: list[tuple[float, str, int, str]] = []
        failed_rows: list[tuple[int, str]] = []
        for link in chunk:
            judgment = by_key.get((link.article_id, link.ticker))
            p = _valid_probability(judgment.p_relevant) if judgment is not None else None
            if judgment is not None and p is not None:
                ok_rows.append((p, judgment.judge_version, link.article_id, link.ticker))
            else:
                failed_rows.append((link.article_id, link.ticker))
                if len(sample_errors) < 3:
                    error = judgment.error if judgment is not None else None
                    sample_errors.append(error or "no judgment")

        with get_connection() as conn:
            with conn.cursor() as cur:
                if ok_rows:
                    cur.executemany(_WRITE_JUDGED_SQL, ok_rows)
                if failed_rows:
                    cur.executemany(mark_failed_sql, failed_rows)
            conn.commit()

        judged += len(ok_rows)
        failed += len(failed_rows)
        if chunk and not ok_rows:
            aborted = True
            logger.warning(
                "%s: every link in a chunk of %d failed — stopping this run "
                "(%d links left unjudged)", caller, len(chunk), len(links) - i - len(chunk),
            )
            break

    attempted = judged + failed
    stats = JudgeRunStats(
        selected=len(links),
        judged=judged,
        failed=failed,
        cap_remaining=remaining - attempted,
        aborted=aborted,
    )
    if attempted and failed / attempted > FAILURE_WARN_RATE:
        logger.warning(
            "%s: %d/%d links failed (%s), e.g. %s",
            caller, failed, attempted, judge.version, sample_errors,
        )
    else:
        logger.info(
            "%s: judged %d, failed %d of %d selected (%s)",
            caller, judged, failed, len(links), judge.version,
        )
    return stats


# ---------- drift (read-only) ------------------------------------------------


NEAR_HALF = (0.4, 0.6)         # "the judge is unsure" band for the drift check

_DRIFT_SQL = f"""
    WITH links AS (
        SELECT ac.article_id,
               ac.relevance_judged    AS judged,
               ac.relevance_judge     AS judge,
               ac.relevance_judged_at AS judged_at,
               COALESCE(ac.relevance_judged, ac.relevance) AS effective,
               date_trunc('week', a.published_at)::date    AS week
        FROM article_companies ac
        JOIN articles a ON a.id = ac.article_id
        WHERE a.published_at >= date_trunc('week', now()) - make_interval(weeks => %(weeks)s)
          {{ticker_clause}}
    ), by_link AS (
        SELECT week,
               count(*)                                          AS n_links,
               count(judged)                                     AS n_judged,
               count(*) FILTER (WHERE judged_at IS NOT NULL)     AS n_attempted,
               count(*) FILTER (WHERE judged_at IS NOT NULL AND judged IS NULL) AS n_failed,
               percentile_cont(ARRAY[0.1, 0.25, 0.5, 0.75, 0.9])
                   WITHIN GROUP (ORDER BY judged)                AS p_quantiles,
               count(*) FILTER (WHERE judged >= {NEAR_HALF[0]} AND judged < {NEAR_HALF[1]})
                                                                 AS n_near_half,
               count(*) FILTER (WHERE judged >= %(threshold)s)   AS n_judged_pass,
               string_agg(DISTINCT judge, ',' ORDER BY judge)    AS judges
        FROM links
        GROUP BY week
    ), by_article AS (
        SELECT week,
               count(*)                                          AS n_articles,
               count(*) FILTER (WHERE max_effective >= %(threshold)s) AS n_articles_pass
        FROM (SELECT week, article_id, max(effective) AS max_effective
              FROM links GROUP BY week, article_id) per_article
        GROUP BY week
    )
    SELECT l.week, l.n_links, l.n_judged, l.n_attempted, l.n_failed, l.p_quantiles,
           l.n_near_half, l.n_judged_pass, a.n_articles, a.n_articles_pass, l.judges
    FROM by_link l
    JOIN by_article a USING (week)
    ORDER BY l.week
"""


@dataclass(frozen=True)
class WeeklyDrift:
    """One ISO week (by ``articles.published_at``) of judge health.

    ``failure_rate`` counts links whose latest attempt failed and that are
    still unjudged, over links attempted at all. ``p_quantiles`` are the
    10/25/50/75/90th percentiles of ``relevance_judged`` (judged links only).
    ``article_pass_share`` is the gate's view: articles with at least one link
    whose *effective* relevance clears the threshold.
    """

    week: date
    n_links: int
    n_judged: int
    n_attempted: int
    n_failed: int
    p_quantiles: tuple[float, ...] | None
    n_near_half: int
    n_judged_pass: int
    n_articles: int
    n_articles_pass: int
    judges: tuple[str, ...]

    @property
    def judged_share(self) -> float | None:
        return self.n_judged / self.n_links if self.n_links else None

    @property
    def failure_rate(self) -> float | None:
        return self.n_failed / self.n_attempted if self.n_attempted else None

    @property
    def near_half_share(self) -> float | None:
        return self.n_near_half / self.n_judged if self.n_judged else None

    @property
    def judged_pass_share(self) -> float | None:
        return self.n_judged_pass / self.n_judged if self.n_judged else None

    @property
    def article_pass_share(self) -> float | None:
        return self.n_articles_pass / self.n_articles if self.n_articles else None

    @property
    def p_median(self) -> float | None:
        return self.p_quantiles[2] if self.p_quantiles else None


def weekly_drift(
    *,
    weeks: int = 8,
    threshold: float | None = None,
    tickers: Sequence[str] | None = None,
) -> list[WeeklyDrift]:
    """Read-only per-week judge health over the last ``weeks`` full weeks
    plus the current, partial one (last in the list).

    ``threshold`` defaults to ``RELEVANCE_THRESHOLD``. ``tickers`` restricts
    the links (pass the covered universe so links written for other tickers
    don't read as unjudged). Nothing is written.
    """
    params: dict[str, Any] = {
        "weeks": weeks,
        "threshold": get_settings().relevance_threshold if threshold is None else threshold,
    }
    ticker_clause = ""
    if tickers is not None:
        ticker_clause = "AND ac.ticker = ANY(%(tickers)s)"
        params["tickers"] = sorted(set(tickers))
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(_DRIFT_SQL.format(ticker_clause=ticker_clause), params)
        rows = cur.fetchall()
    return [
        WeeklyDrift(
            week=week, n_links=int(n_links), n_judged=int(n_judged),
            n_attempted=int(n_attempted), n_failed=int(n_failed),
            p_quantiles=tuple(float(q) for q in quantiles) if quantiles else None,
            n_near_half=int(n_near_half), n_judged_pass=int(n_judged_pass),
            n_articles=int(n_articles), n_articles_pass=int(n_articles_pass),
            judges=tuple(j for j in (judges or "").split(",") if j),
        )
        for (week, n_links, n_judged, n_attempted, n_failed, quantiles, n_near_half,
             n_judged_pass, n_articles, n_articles_pass, judges) in rows
    ]
