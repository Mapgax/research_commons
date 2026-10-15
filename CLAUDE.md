# research_commons — Claude Context

Shared Python library for the **MSARN / Companies_News / Idee_Scraping** ecosystem,
and the home of the central ticker-universe workflow.

Remote: `github.com/Mapgax/research_commons`. All three consumer projects install it
as a git dependency pinned to `@main`:

```
research-commons @ git+https://github.com/mapgax/research_commons.git@main
```

**Pushing to `main` therefore changes all three production projects immediately.**
There is no staging branch and no deploy pipeline for this repo — the push *is* the
release. Consumers pick it up on their next install/build (Railway rebuild, GHA run).

⚠️ **"Immediately" is not automatic — a rebuild is not enough.** `@main` is
resolved by pip at *build* time, so a cached build layer leaves a consumer
running current app code against a stale copy of this library. That is exactly
what broke the 2026-08-27 Companies_News briefing: the container had rebuilt
(new app code, new `tickers.csv`) but still carried a `research_commons` without
`scope_policy`, and the swallowed ImportError shrank its universe to three
names. **After adding anything a consumer imports, redeploy that consumer
without build cache**, or force it:

```bash
pip install --upgrade --force-reinstall --no-deps 'research-commons @ git+https://github.com/mapgax/research_commons.git@main'
```

Adding a symbol is the dangerous direction: consumers written against it fail
on the stale copy. Changing an existing symbol's behaviour propagates the same
way but fails silently instead.

## Ecosystem map

This repo lives at `~/Projects/AI/research_commons` — moved there from
`~/Desktop/RBZ_Projects/` on 2026-07-29, so it now sits beside MSARN. It is
`pip install -e`'d into system Python 3.12; **that editable install hardcodes the
path**, so moving this checkout again requires re-running `pip install -e ".[dev]"`
from the new location or every `import research_commons` breaks.

| Project | Path | Uses this library for |
|---|---|---|
| MSARN | `~/Projects/AI/project_MSARN` (**not** under RBZ_Projects) | market DB connection, ticker registry via generated `tickers.csv` |
| Companies_News | `~/Desktop/RBZ_Projects/Companies_News` | news DB reads/writes, LLM client, source registry |
| Idee_Scraping | `~/Desktop/RBZ_Projects/Idee_Scraping` | news DB, `resolve_alias` / `upsert_ticker_alias` |
| risk_monitor | `~/Projects/AI/risk_monitor` | **nothing** — it is a `distribute` target only |

risk_monitor receives `tickers.csv` (it is in `universe_targets.toml`) but does
**not** depend on this package, so it cannot import `scope_policy` and restates
the tier rule locally in `scoring.py`, `app.py` and `research/run_markov_wf.py`.
That is the one accepted exception to the "import, don't restate" rule below;
adding the dependency for one lookup table has not been judged worth it. Its
cadence happens to match (daily `active`, Saturday `covered`) — keep the two in
step by hand.

Idee_Scraping is **intentionally not** a `distribute` target and has no
`tickers.csv` — its value is discovering companies outside any fixed list, so
it stays scope-agnostic by design. Don't add it to `universe_targets.toml`.

Two Aiven Postgres databases: **`market`** (prices, fundamentals, features,
`ticker_metadata`) and **`news`** (articles, classifications, sentiment, aliases).

## Layout / entry points

- `research_commons/db_market`, `db_news` — connection pools, reads, writes, DDL.
- `research_commons/universe/` — the central ticker-universe CLI (see below).
- `research_commons/tickers.py` — canonical registry API + the consolidated
  Bloomberg→Yahoo / Bloomberg→currency maps and manual overrides. This is the **one**
  place those maps live; per-project copies were removed in July 2026.
- `docs/UNIVERSE_DICTIONARY.md` — field-by-field dictionary for both canonical
  universe files (`ticker_universe.csv`, `email_focus_tickers.csv`): what each
  column means, who writes it, what each `scope_status` tier actually costs and
  gets, and an amendment log. Read this before changing a tier or an audience flag.
- `research_commons/scope_policy.py` — the `stage -> scope_status tiers` table
  (`scopes_for`, `includes`, `stages_for`). Pure: no DB, no pandas, no I/O,
  because consumer runtime is CSV-authoritative. The one definition of which
  pipeline stages run for which tier — see the coverage rules below.
- `research_commons/source_health/` — independent homepage monitor.
- `research_commons/db_news/relevance.py` — the relevance gate
  (`docs/spec-jev-relevance-gate.md`): a `RelevanceJudge` protocol with
  `HaikuRelevanceJudge` / `JevRelevanceJudge`, and `judge_pending_links()`,
  which writes `article_companies.relevance_judged*` next to the string-match
  prior. Read relevance through `reads.effective_relevance_sql()` /
  `load_article_links()` — never restate `COALESCE(relevance_judged, relevance)`.
  Off by default (`RELEVANCE_JUDGE=none`). **The Jev judge sends article
  excerpts to TypeSafe and refuses to construct without
  `RELEVANCE_JEV_OPT_IN=true`, which needs the spec's Phase 0 privacy
  sign-off first.** `refresh_sentiment_daily` also fills the weighted
  `sentiment_daily` columns; the original columns keep their meaning.
  Offline eval: `scripts/relevance_eval.py` (data in git-ignored `eval/`).
  After go-live, follow `docs/relevance-gate-runbook.md`: a weekly read-only
  `scripts/relevance_gate.py drift`, a monthly `relevance_eval.py sample
  --top-up 50` + `run` (appends to `eval/relevance/history.csv`), and threshold
  moves (`RELEVANCE_THRESHOLD` on Companies_News `morning-collect` and
  `evening-collect`). Re-judging after a `HAIKU_JUDGE_VERSION` bump uses
  `relevance_gate.py rejudge --since …` (plan-only without `--apply`).
- `migrations/market/*.sql`, `migrations/news/*.sql` — the only schema source.
- `reports/ticker_universe_report.py` — read-only Streamlit dashboard: ticker
  metadata (scope_status, sector, email-focus flags) joined against per-table
  row counts/date ranges (prices, fundamentals, features, alt_data,
  insider_transactions, option_metrics), plus a data-quality alerts tab
  (zero rows, below the 60-row feature-engineering minimum, stale prices,
  missing features). Not a package dependency — streamlit stays out of
  `pyproject.toml`; install it separately if missing. Launch via
  `reports/Launch Ticker Universe Report.command` (double-click in Finder) or
  the Runbook command below.

## Runbook

```bash
# setup
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e ".[dev]"        # add ,universe for .xlsx ingest support

# tests (no DB needed — all unit tests use fake connections)
python3 -m pytest tests/ -q

# connectivity smoke test (pings both DBs + 1-token LLM call)
python3 scripts/check_connections.py

# apply schema migrations (idempotent, safe to re-run)
python3 -c "import research_commons; research_commons.init_market_db()"
python3 -c "import research_commons; research_commons.init_news_db()"

# source health monitor
python3 -m research_commons.source_health --skip-email

# ticker universe coverage & freshness report (Streamlit; needs `pip install streamlit`)
streamlit run reports/ticker_universe_report.py
# or just double-click reports/Launch Ticker Universe Report.command
```

Env vars must be exported before DB commands (`set -a && source .env && set +a`).

## Ticker universe — the central workflow

`data/ticker_universe.csv` (repo root, git-versioned) is the canonical universe.
It is mirrored into `market.ticker_metadata` and distributed as `tickers.csv` into
the consumer repos listed in `universe_targets.toml`.

```bash
# general update: additive, new names enter as watchlist (zero collection cost)
python3 -m research_commons.universe ingest universe_update_YYYYMMDD.xlsx --mode universe

# focus update: snapshot — in file → active, previously-active absent → inactive
python3 -m research_commons.universe ingest focus_update_YYYYMMDD.csv --mode focus

python3 -m research_commons.universe verify          # canonical vs DB drift check
python3 -m research_commons.universe distribute --apply
python3 -m research_commons.universe sync-db
python3 -m research_commons.universe rename-ticker OLD NEW --apply
```

**`--apply` now commits and pushes each consumer repo automatically** (since
2026-07-30 — `push_targets()` in `dist.py`), for `ingest --apply`,
`distribute --apply`, `rename-ticker --apply`, and `update-email-focus --apply`
alike. A push failure doesn't undo the local write or commit — it's reported
per-target so you can push that one manually. Pass `--no-push` to commit
locally without pushing (e.g. to review the diff before it ships). This repo's
own canonical-file commit is never automated — only the consumer repos are.

Rules that must not be broken:

- **This tool is the only writer to `market.ticker_metadata`.** MSARN's
  `sync_registry_metadata()` was reduced to a read-only drift check, and
  `ticker_metadata` was removed from its `MARKET_TABLE_MIGRATION_COLUMNS` map, so no
  second schema manager patches that table. Don't reintroduce either. **Convention
  only, not DB-enforced**: every consumer connects as `avnadmin`, which owns every
  table (owners bypass GRANT/REVOKE), and `db_market`/`db_news` `get_connection()`
  is shared indiscriminately between reads and writes — e.g. Companies_News writes
  its source registry and LLM classifications through the same function a read call
  would use. `sector_reader` has real SELECT-only grants on both DBs (2026-07-30)
  and `MARKET_ROLE`/`NEWS_ROLE` will `SET ROLE` to it when set, but do **not** set
  either in a real consumer `.env` — it would silently break every write on that
  connection. Actual enforcement needs a distinct read-only entrypoint (or
  per-callsite role switching), not a blanket env var; that's unbuilt.
- Ingest is **plan-only by default**; `--apply` writes (and, unless `--no-push`
  is passed, pushes). Quarantined issues are never merged silently.
- Merge key is **ISIN**, never the ticker. An existing row's ticker changes only via
  `rename-ticker` (which relies on `ON UPDATE CASCADE` across the six FK tables —
  `prices`, `fundamentals`, `features`, `alt_data`, `insider_transactions`,
  `option_metrics`). Hand-editing a ticker in the CSV orphans its history.
- **Currency is never auto-refreshed.** Upload files carry `DenCurrency`, a
  *denomination* currency that is frequently wrong about the trading currency
  (EUR for LSE names, USD for Hong Kong lines). Mismatches are reported, not applied.
- `scope_status` is the single **coverage** dial, and
  `research_commons/scope_policy.py` is the single place its meaning is defined —
  a `stage -> tiers` table plus `scopes_for()` / `includes()`. Consumers that
  depend on this package must import that meaning, not restate it; before
  2026-08-23 the rule was duplicated in six places across the three repos and
  drifted from the docs. risk_monitor is the documented exception — see the
  ecosystem map above.
  - `active` — everything, and scored **daily** (MSARN's `SCOPE_FOCUS` overview
    merge, launchd 06:00; risk_monitor's 06:30 `active` run). Scoring *cadence*
    is the only thing distinguishing it from `inactive`.
  - `inactive` — prices, fundamentals, features, alt-data, options and news, all
    on the same cadence as `active`; scored only in the weekly `SCOPE_FULL`
    rebuild (launchd Sundays 01:00), so its risk numbers can be a week old.
    This is the bulk of the universe.
  - `watchlist` — prices and fundamentals only, on the recurring cadence:
    quant-analysable for equity research, but no features, alt-data, options or
    news. Cheap to hold, instant to promote.
  Corrected 2026-08-27: this used to say `inactive` was "never scored", and
  `scope_policy` had `scoring -> {active}`. MSARN's weekly full rebuild scores
  the whole covered universe, so the table now carries two stages — `scoring`
  (active + inactive) and `scoring_daily` (active only).
  It does **not** govern email delivery — see the email-focus workflow below,
  with the one exception recorded there.
- The CLI refuses to run from an installed copy — `data/` and `reports/` are repo-root
  artifacts, deliberately outside the package.

## Email-focus audience — PM/advisory delivery, separate from coverage

`data/email_focus_tickers.csv` (repo root, git-versioned, `;`-delimited) is a
**second, deliberately independent dial**: who gets emailed, as opposed to
`scope_status` above, which governs collection/coverage only. A ticker can be
covered (`scope_status=active`) and never emailed, or emailed regardless of
its coverage status — the two are unrelated.

The old `scope_pm_ch` / `scope_pm_de` / `scope_advisory` audience flags used
to live as columns on the main universe file; they were retired from there on
2026-07-29 (values archived in `data/archive/retired_scope_flags_20260729.csv`)
and reinstated the same day as this standalone file instead, after the
retirement turned out to remove audience targeting the email pipeline still
needed. **Do not re-add these columns to `ticker_universe.csv`** — this file
is the one and only place they live now.

Columns: `ISIN;company_name;scope_pm_ch;scope_pm_de;scope_advisory;email_scope`.
An ISIN absent from the file counts as `0` for all four flags, including
`email_scope` (i.e. excluded from the email by default). `email_scope` is a
real, independently-editable column — populated equal to `scope_pm_ch` by
convention today, but the file is the source of truth and the two can diverge.

```bash
# merge an uploaded PM/advisory file (universe_update_xlsx_files/, or an
# explicit path) into data/email_focus_tickers.csv, then distribute it to
# every target flagged email_focus=true in universe_targets.toml
python3 -m research_commons.universe update-email-focus [file] --apply
```

**One invariant crosses the two dials**: an ISIN with `email_scope=1` must be
`scope_status=active`. A name on the delivery list is one the desk is working,
so its risk numbers have to be same-day fresh, and only `scoring_daily` runs
every day. `update-email-focus` promotes violators as part of its plan (and, on
`--apply`, writes `ticker_universe.csv`, the DB mirror and every consumer's
`tickers.csv` alongside the audience file); `verify` reports them read-only and
exits non-zero. More than `ACTIVATION_THRESHOLD` promotions needs `--yes`, the
same cost gate `ingest` applies. Delivery still does not imply coverage in the
other direction — an `active` ticker is not emailed unless it is flagged here.

Plan-only by default (omit `--apply`). An ISIN that drops out of the latest
upload isn't silently zeroed: the command prints its current values and asks
whether to keep them or reset to `0` — except `scope_advisory`, which always
resets to `0` on removal with no prompt (missing means false for advisory
specifically, not for the PM/email flags). A non-interactive stdin (e.g. cron)
defaults to "keep" rather than blocking.

Companies_News reads `email_scope` (not `scope_status`) to decide who gets the
briefing email — see `load_email_focus_tickers()` / `email_focus_delivery_tickers()`
there.

## Migrations

`migrations/<db>/NNNN_snake_name.sql`, applied in filename order by
`init_market_db` / `init_news_db`. There is no version table — **idempotency is the
mechanism**, so every statement must be guarded (`IF NOT EXISTS`, or a `DO $$` block
checking `pg_constraint` / `information_schema`). Postgres has no
`ADD CONSTRAINT IF NOT EXISTS`; using it silently rolls back the whole migration
(that bug shipped once — see `0002_scope_groups.sql`'s header).

## Secrets (names only — values live in `.env`, never committed)

`MARKET_DATABASE_URL`, `NEWS_DATABASE_URL`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`LLM_PRIMARY_MODEL`, `LLM_FALLBACK_MODEL`, `RC_LOG_LEVEL`, `SOURCE_HEALTH_*`,
`RESEND_API_KEY`, `RESEND_FROM`, `EMAIL_TO`, `EMAIL_USER`, `EMAIL_PASS`,
`TYPESAFE_API_KEY`, `RELEVANCE_JUDGE`, `RELEVANCE_THRESHOLD`, `RELEVANCE_DAILY_CAP`,
`RELEVANCE_JEV_OPT_IN`, `RELEVANCE_JEV_ALLOW_FALLBACK`.

## CI

`.github/workflows/source_health.yml` — weekly Mondays 06:00 UTC plus manual
dispatch; runs the source-health monitor against `news_db` and emails the report.
No deployment workflow exists in this repo.

## Conventions

- Keep runtime dependencies **minimal** (`pyproject.toml`); heavy/optional deps go in
  an extra (`[universe]` for openpyxl) or stay in the consumer projects.
- ruff line-length 100, mypy strict, `from __future__ import annotations`.
- Tests are pure unit tests: hand-rolled `FakeCursor`/`FakeConnection` injected with
  `monkeypatch.setattr(module, "get_connection", ...)`. No test touches a real DB.
