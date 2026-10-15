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
    HaikuRelevanceJudge(client=None, *, batch_size=10)
    JevRelevanceJudge(*, api_key=None, opt_in=None, allow_fallback=None,
                      transport=None, fallback_transport=None, max_workers=8)
    make_relevance_judge(settings=None) -> RelevanceJudge | None
    judge_pending_links(judge, *, company_names, limit=None, since=None,
                        daily_cap=None) -> JudgeRunStats

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
from datetime import datetime
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

HAIKU_JUDGE_VERSION = "haiku-v1"
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
    "`article` and a `company`. For each item answer this question:\n"
    f"{RELEVANCE_QUESTION}\n"
    "Give the probability that the answer is yes, as a float from 0 to 1.\n"
    'Return ONLY a JSON array with one object per item: {"id": <the integer id, '
    'copied back exactly>, "p_relevant": <float 0..1>}. No markdown. Every item '
    "must get exactly one object with its matching id."
)


class HaikuRelevanceJudge:
    """Company-aware relevance prompt through the shared :class:`LLMClient`.

    Batches links and matches answers by the echoed id, never by position —
    the same pattern as Companies_News ``run_llm_enrichment``.
    """

    version = HAIKU_JUDGE_VERSION

    def __init__(self, client: LLMClient | None = None, *, batch_size: int = 10) -> None:
        if client is None:
            from research_commons.llm import LLMClient

            client = LLMClient()
        self._client = client
        self._batch_size = batch_size
        self.usage = JudgeUsage()

    def judge(self, links: Sequence[RelevanceLink]) -> list[RelevanceJudgment]:
        out: list[RelevanceJudgment] = []
        for i in range(0, len(links), self._batch_size):
            out.extend(self._judge_batch(links[i:i + self._batch_size]))
        return out

    def _judge_batch(self, batch: Sequence[RelevanceLink]) -> list[RelevanceJudgment]:
        items = [
            f"[{idx}] {json.dumps(link_state(link), ensure_ascii=False)}"
            for idx, link in enumerate(batch)
        ]
        self.usage.calls += 1
        try:
            result = self._client.generate(
                "\n".join(items),
                system=_HAIKU_SYSTEM,
                response_format="json",
                max_output_tokens=64 + 32 * len(batch),
            )
        except Exception as exc:
            self.usage.failed_calls += 1
            logger.warning("Haiku relevance judge: batch of %d failed: %s", len(batch), exc)
            return [_failed(link, self.version, f"llm error: {exc}") for link in batch]

        self.usage.input_tokens += result.input_tokens
        self.usage.output_tokens += result.output_tokens
        self.usage.latencies_ms.append(result.latency_ms)
        version = f"{self.version}/{result.model_used}"

        answers = result.content
        if isinstance(answers, dict):
            answers = [answers]
        if not isinstance(answers, list):
            return [_failed(link, version, "response is not a JSON array") for link in batch]

        by_id: dict[int, float | None] = {}
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            answer_id = answer.get("id")
            if isinstance(answer_id, bool) or not isinstance(answer_id, int):
                continue
            if not 0 <= answer_id < len(batch) or answer_id in by_id:
                continue  # out of range, or a duplicate: keep the first
            by_id[answer_id] = _valid_probability(answer.get("p_relevant"))

        out: list[RelevanceJudgment] = []
        for idx, link in enumerate(batch):
            if idx not in by_id:
                out.append(_failed(link, version, "no answer with this id"))
            elif by_id[idx] is None:
                out.append(_failed(link, version, "p_relevant missing or outside 0..1"))
            else:
                out.append(RelevanceJudgment(link.article_id, link.ticker, by_id[idx], version))
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
    names = {t: n.strip() for t, n in company_names.items() if t and n and n.strip()}
    if not names:
        logger.warning("judge_pending_links: no company names given — nothing to judge")
        return JudgeRunStats(0, 0, 0, 0)
    cap = get_settings().relevance_daily_cap if daily_cap is None else daily_cap

    params: dict[str, Any] = {"tickers": sorted(names)}
    since_clause = ""
    if since is not None:
        since_clause = "AND a.published_at >= %(since)s"
        params["since"] = since

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_ATTEMPTS_TODAY_SQL)
            remaining = max(cap - int(cur.fetchone()[0]), 0)
            if remaining == 0:
                logger.warning(
                    "judge_pending_links: daily cap of %d judge attempts reached", cap,
                )
                return JudgeRunStats(0, 0, 0, 0)
            params["limit"] = remaining if limit is None else min(limit, remaining)
            cur.execute(_PENDING_SQL.format(since_clause=since_clause), params)
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
            logger.warning("judge_pending_links: judge raised: %s", exc)
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
                    cur.executemany(_MARK_FAILED_SQL, failed_rows)
            conn.commit()

        judged += len(ok_rows)
        failed += len(failed_rows)
        if chunk and not ok_rows:
            aborted = True
            logger.warning(
                "judge_pending_links: every link in a chunk of %d failed — stopping "
                "this run (%d links left unjudged)", len(chunk), len(links) - i - len(chunk),
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
            "judge_pending_links: %d/%d links failed (%s), e.g. %s",
            failed, attempted, judge.version, sample_errors,
        )
    else:
        logger.info(
            "judge_pending_links: judged %d, failed %d of %d selected (%s)",
            judged, failed, len(links), judge.version,
        )
    return stats
