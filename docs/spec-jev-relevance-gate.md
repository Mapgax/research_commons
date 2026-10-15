# Spec: Jev relevance gate for news_db

Status: draft, 2026-10-15. **Blocked on Phase 0 (privacy sign-off).** No code
or eval call may send article content to TypeSafe or OpenRouter until it is
recorded.

## Problem Statement

Every consumer of `news_db` — the Companies_News briefing, MSARN sentiment
features, risk_monitor, and the equity agent — treats an (article, ticker)
link as if the article were about that company. Mostly it isn't:

- `article_companies.relevance` is a string-match prior written at sync time:
  1.0 if the ticker or the *first word* of the company name appears in the
  title, 0.6 if only in the body, 0.3 if neither. Over the 30 days to
  2026-10-15, 27,137 of ~80k links (34%) sat at 0.3 — the article never names
  the company. First-word matching also yields false 1.0s for names like
  "General …" or "American …".
- Nothing downstream uses that prior except the equity agent's evidence
  packet, which sorts by it. `sentiment_daily` averages every link with equal
  weight, so off-topic articles move the sentiment and severity that MSARN,
  risk_monitor and the equity agent's screener read.
- Classification (event type, severity, sentiment) is capped at 200 articles
  per run against ~2,650 new articles a day, so only ~17% are ever classified,
  and articles are picked newest first with no regard to relevance. An
  unclassified article is absent from `sentiment_daily`. The cap is spent on
  noise while relevant articles go unscored.
- Classification never sees which company it is classifying for, so severity
  and sentiment are generic to the article, not specific to the ticker.

## Solution

Add a **relevance gate** to `research_commons.db_news`: a typed, per
(article, ticker) judgment of "is this article materially about this
company?" made by a cheap judge, stored next to the existing prior, and used
in two places:

1. **Gate classification.** Companies_News spends its classification budget
   on links that pass the gate, most relevant first, instead of the newest
   articles regardless of relevance.
2. **Weight the daily aggregate.** `sentiment_daily` gains relevance-weighted
   columns alongside the existing ones, so each consumer opts in deliberately
   rather than having its inputs change silently via `@main`.

The judge sits behind one interface with two implementations: **Jev**
(TypeSafe System One, `noul` primitive) and **Haiku** (via the existing
`LLMClient`, company-aware prompt). An offline eval on a hand-labelled set
decides which goes to production. If Haiku wins or ties on quality at
acceptable cost, ship Haiku and avoid adding a vendor; the gate design is
identical either way.

## Phases

| Phase | What | Exit criterion |
|---|---|---|
| 0 | Privacy sign-off | Decision recorded (see below). If no, Jev is dropped; the eval and gate proceed Haiku-only. |
| 1 | Prerequisite fix + schema | `upsert_article_companies` no longer wipes other tickers' links; new columns migrated |
| 2 | Judge interface + offline eval | Eval report with metrics for prior / Haiku / Jev on the labelled set; judge chosen |
| 3 | Gate in production | Companies_News judges new links and gates classification; shadow mode first |
| 4 | Weighted aggregate | `sentiment_daily` weighted columns populated and backfilled; consumers migrate one at a time |

### Phase 0 — privacy sign-off (blocking)

Companies_News lives under `~/Desktop/RBZ_Projects`, and the Jev skill's
standing rule is that content from there is not sent to Jev. The articles are
public news that already go to Anthropic and Gemini under the Companies_News
data-flow table, but TypeSafe — and OpenRouter as its fallback — would be
**new processors**. Andreas decides, and if R&Co policy requires it, gets
compliance's OK. The decision must cover:

- **What is sent:** article title, the first 1,000 characters of the body, the
  ticker, and the company name. Nothing else: no `scope_status`, no
  `email_scope`/PM flags, no portfolio, holdings or client data.
- **What it reveals:** even without flags, the stream of (ticker, company)
  pairs exposes the ~441-name coverage universe to the processor. Acceptable
  or not?
- **Fallback:** whether OpenRouter is an allowed fallback. Default in this
  spec: **no** — the Jev judge runs TypeSafe-only, and a TypeSafe failure
  leaves the link unjudged (it falls back to the prior), never routed
  elsewhere.
- **Retention:** check TypeSafe's data-retention and training-use terms
  before signing off.

Recording the decision: one line in Companies_News `CLAUDE.md` → *External
Data Flows* (`TypeSafe (Jev) | title, 1k-char body excerpt, ticker, company
name`), plus this spec's status line. In code, the Jev judge refuses to run
unless an explicit opt-in setting is true (see Implementation Decisions), so
an unreviewed deploy cannot start sending data by accident.

The eval in Phase 2 sends the same content, so **Phase 0 gates the eval too**.
The Haiku arm, the labelling and the regex-prior arm can start immediately.

## User Stories

1. As Andreas, I want to sign off on sending public article text to TypeSafe before any call is made, so that a new processor is never added to an R&Co pipeline by default.
2. As Andreas, I want the Jev judge to refuse to run unless it has been explicitly enabled, so that a deploy without sign-off cannot leak data.
3. As Andreas, I want OpenRouter fallback off by default for this judge, so that a TypeSafe outage does not route work data through a second processor I did not approve.
4. As Andreas, I want the exact fields sent to the judge to be fixed by the library, so that no caller can add PM flags or portfolio context to the payload.
5. As Andreas, I want to label a stratified sample of real links in a local CSV, so that the eval measures what "relevant" means for this coverage list.
6. As Andreas, I want to compare the regex prior, Haiku and Jev on the same labelled links, so that the choice of judge rests on numbers, not on novelty.
7. As Andreas, I want the eval to report cost per 1,000 links and latency next to quality, so that I can weigh a cheaper judge against a slightly better one.
8. As Andreas, I want the eval to show where judges disagree with my labels, so that I can see whether errors are in ambiguous cases or in obvious ones.
9. As Andreas, I want a written decision rule for picking the judge before the eval runs, so that I cannot rationalise the result afterwards.
10. As Andreas, I want the gate to work with Haiku alone, so that a "no" on privacy does not kill the improvement.
11. As the Companies_News collect job, I want to judge only links that have no judgment yet, so that each link is paid for once.
12. As the Companies_News collect job, I want to spend the classification budget on the most relevant unclassified links first, so that the 17% coverage goes to articles that matter.
13. As the Companies_News collect job, I want links below the gate threshold to stay stored but unclassified, so that nothing is deleted and a later threshold change can revisit them.
14. As the Companies_News collect job, I want a judge failure to leave the link unjudged and the run to continue, so that an outage degrades to today's behaviour instead of failing collection.
15. As the Companies_News collect job, I want judge failures and counts logged at WARNING when they pass a threshold, so that a silent judge outage does not look like a quiet news week.
16. As the Companies_News briefing, I want to prefer judged relevance over the string-match prior when ranking a company's events, so that commentary that merely mentions the company stops crowding out real news.
17. As MSARN, I want relevance-weighted sentiment as new columns, so that my features change only when I choose to switch and retrain.
18. As MSARN, I want a backfilled history for the weighted columns, so that I can retrain on a consistent series instead of a series that changes definition mid-way.
19. As risk_monitor, I want an article count that counts only relevant articles, so that an "article surge" alert reflects news about the company, not noise.
20. As the equity agent, I want each article in my evidence packet to carry judged relevance and which judge produced it, so that I can filter and cite with confidence.
21. As any consumer, I want the existing `sentiment_daily` columns to keep their current meaning, so that pushing this to `main` changes nothing until I opt in.
22. As any consumer, I want a single "effective relevance" read helper, so that every project resolves judged-vs-prior the same way.
23. As the library maintainer, I want the judge version stored with each judgment, so that a later judge or prompt change can be re-run selectively.
24. As the library maintainer, I want re-syncing an article not to erase its judgment or other tickers' links, so that paid judgments are not silently thrown away.
25. As the library maintainer, I want the judge behind one small interface, so that swapping Jev for Haiku (or a future model) is a config change, not a refactor.
26. As the library maintainer, I want unit tests that use a fake judge and fake connections, so that the suite stays DB- and network-free like the rest of `tests/`.
27. As Andreas, I want a shadow period where judgments are written but not yet used for gating, so that I can compare gated and ungated classification coverage before switching.
28. As Andreas, I want a daily cap on judge calls, so that a backlog or bug cannot run up an unbounded TypeSafe or Anthropic bill.

## Implementation Decisions

**Seam.** One new module in `research_commons.db_news` owning relevance:
a `RelevanceJudge` protocol with a single batch method — links in,
judgments out — plus `judge_pending_links(judge, *, limit)` which reads
unjudged links, calls the judge, and writes results. Two implementations:
`JevRelevanceJudge` and `HaikuRelevanceJudge`. The offline eval and the
production path call the same protocol; that is the seam everything is
tested at. *To confirm with Andreas before building:* placing the judge here
rather than in Companies_News, on the grounds that the column it writes is
shared by every consumer and any future writer (e.g. Idee_Scraping) should
use the same judge.

Judgment shape (decision-rich part):

```
RelevanceLink:      article_id, ticker, company_name, title, body_excerpt (≤1000 chars)
RelevanceJudgment:  article_id, ticker, p_relevant (0..1) | None, judge_version, error | None
```

`p_relevant` is a probability, not an intensity. `None` means unjudged
(failure or refused) and the prior stays in effect.

**Jev question.** One `noul` per link: "Is the article in `article` primarily
about `company.name` (ticker `company.ticker`), or does it report an event,
result, decision or development specific to that company? Answer no if the
company is only listed, quoted as an example, mentioned in passing, or if the
name matches a different entity." State is a JSON object
`{article: {title, body_excerpt}, company: {ticker, name}}`. Independent links
go in parallel calls; questions are not batched across links because each
link has its own state. TypeSafe-only; no OpenRouter fallback unless Phase 0
explicitly allows it.

**Haiku judge.** Same definition as a prompt via the existing `LLMClient`,
returning a JSON probability per id, batched like `run_llm_enrichment`'s
id-echo pattern (match by echoed id, never by position). Uses the current
Haiku model configured in `LLMClient`.

**Configuration.** New settings in `research_commons.config`: judge choice
(`none` | `haiku` | `jev`, default `none`), Jev opt-in flag (default false —
Jev judge raises on construction if false), allow-fallback flag (default
false), daily call cap, gate threshold (initial value set by the eval). The
TypeSafe key is read from an env var, never hardcoded.

**Schema (new migration `0010_relevance_judgment.sql`, additive only).**
On `article_companies`: `relevance_judged DOUBLE PRECISION` (0..1, CHECK),
`relevance_judge TEXT`, `relevance_judged_at TIMESTAMPTZ`. The existing
`relevance` column keeps the string-match prior unchanged. Partial index on
links where `relevance_judged IS NULL` for the pending-links scan.
On `sentiment_daily`: `n_articles_relevant INTEGER`,
`sentiment_mean_weighted DOUBLE PRECISION`, `severity_max_relevant INTEGER`,
`relevance_judge TEXT`. Existing columns keep today's definition.

**Effective relevance.** `COALESCE(relevance_judged, relevance)`, exposed as
one read helper in `db_news.reads` so consumers don't restate it.

**Prerequisite fix — done 2026-10-15 (uncommitted at time of writing).** `upsert_article_companies` currently `DELETE`s every
link for the article before inserting, and Companies_News calls it once per
ticker. An article fetched under two tickers keeps only the last one (likely
why only 20 multi-ticker articles exist in 30 days), and any judgment would
be wiped on re-sync. Change it to an additive upsert that updates the prior
and never touches `relevance_judged*`. Companies_News is the only live caller
(Idee_Scraping has just a vendored, pre-relevance copy under
`proposed_research_commons/`), so additive can be the default; keep a
`replace=True` keyword only if a caller turns up that needs it.

**Gating (Companies_News).** After sync, the collect job calls
`judge_pending_links` (capped), then `run_llm_enrichment` selects unclassified
articles that have at least one link with effective relevance ≥ threshold,
ordered by max effective relevance then recency. Classification becomes
company-aware: the prompt names the linked company. Shadow mode (judge on,
gate off) runs for one to two weeks first; compare classified-relevant counts.

**Weighted aggregate.** `refresh_sentiment_daily` additionally computes
`sentiment_mean_weighted = Σ(rel·s)/Σ(rel)` over links with effective
relevance ≥ threshold, plus the relevant count and max severity. Backfill
judgments for the last 12 months of links (cost estimated from the eval
before running), then backfill the weighted columns.

**Volume and cost.** ~2,600 new links/day, ~80k/month. Backfill ≈ 1M links.
The eval must produce cost per 1k links for both judges so these numbers
can be priced before Phase 3.

## Offline Eval

**Labelled set.** About 300 links from the last 60 days, stratified: 100 each
from prior 0.3 / 0.6 / 1.0, spread across sources (Google News, EDGAR, others)
and across sectors. Plus 30 deliberate hard cases: name collisions, list
articles ("10 stocks to watch"), sector news mentioning several names,
analyst notes. Andreas labels each as relevant / not relevant / unsure;
`unsure` is excluded from the metrics but reported. Labels live in a
**git-ignored** local CSV under an `eval/` directory — it holds R&Co coverage
decisions.

**Arms.** (a) string-match prior, (b) Haiku judge, (c) Jev judge (only after
Phase 0). Same inputs, same fields, same body cut-off.

**Metrics.** ROC-AUC; precision and recall at the threshold that keeps recall
≥ 0.90 (missing relevant news is worse than letting some noise through);
calibration (Brier score, reliability by decile); share of judgments near 0.5;
failure/refusal rate; p50/p95 latency; cost per 1,000 links. Report each per
stratum, especially the 0.3 bucket, where the gate earns its keep.

**Decision rule (fixed now).**
- Jev is adopted only if its AUC ≥ Haiku's − 0.02 **and** its cost per 1k
  links ≤ 50% of Haiku's **and** its failure rate < 2%.
- Otherwise Haiku is adopted, provided it beats the prior's AUC by ≥ 0.05.
- If neither beats the prior by 0.05, stop: weight `sentiment_daily` by the
  existing prior and raise the classification cap instead.

**Output.** A short report (markdown, local, under `eval/`) with the metric
table, the confusion matrix at the chosen threshold, and 20 sampled
disagreements per judge with label and judge probability.

## Testing Decisions

- Test through the public interface: `judge_pending_links` with a fake judge
  and the existing `FakeConnection`/`FakeCursor` pattern from the
  `db_news` write tests — assert SQL params and written values, no DB, no network.
- Judge implementations are tested with stubbed transports: correct state
  shape (and *only* the allowed fields), id-echo matching for Haiku (reordered,
  missing, duplicate ids), `None` on failure, no fallback call when the
  fallback flag is false, refusal to construct Jev without the opt-in flag.
- `upsert_article_companies`: a second ticker does not delete the first; a
  re-sync does not touch `relevance_judged*`.
- `refresh_sentiment_daily`: weighted columns computed from effective
  relevance; existing columns unchanged (regression test on today's SQL output).
- Effective-relevance helper: judged wins over prior; prior used when judged
  is NULL.
- The eval harness itself is a script, not a unit-tested module; its metric
  functions get a small test with a hand-computed example.

## Out of Scope

- Changing `analysis.py`'s materiality thresholds or digest scoring beyond
  preferring effective relevance when ranking events.
- Migrating MSARN, risk_monitor or the equity agent to the weighted columns —
  each is its own change with its own retrain or recalibration.
- Using Jev for event type, severity or sentiment. This spec is relevance only.
- Raising the 200-article classification cap (worth doing; separate decision).
- Idee_Scraping's discovery flow.
- Adding risk_monitor as a research_commons dependency.

## Further Notes

- Pushing to `research_commons@main` releases to every consumer. Ship the
  migration and additive code first, redeploy consumers **without build
  cache**, and only then enable the judge setting in Companies_News.
- Order matters for adding symbols: Companies_News must not import the new
  module until it is on `main` and the consumer has been rebuilt clean.
- The prerequisite `upsert_article_companies` fix is worth doing even if the
  rest of this spec is shelved.
- Jev docs: https://docs.typesafe.ai/llms.txt (`/primitives`, `/confidence`).
