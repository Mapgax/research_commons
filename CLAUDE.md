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

Two Aiven Postgres databases: **`market`** (prices, fundamentals, features,
`ticker_metadata`) and **`news`** (articles, classifications, sentiment, aliases).

## Layout / entry points

- `research_commons/db_market`, `db_news` — connection pools, reads, writes, DDL.
- `research_commons/universe/` — the central ticker-universe CLI (see below).
- `research_commons/tickers.py` — canonical registry API + the consolidated
  Bloomberg→Yahoo / Bloomberg→currency maps and manual overrides. This is the **one**
  place those maps live; per-project copies were removed in July 2026.
- `research_commons/source_health/` — independent homepage monitor.
- `migrations/market/*.sql`, `migrations/news/*.sql` — the only schema source.

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

Rules that must not be broken:

- **This tool is the only writer to `market.ticker_metadata`.** MSARN's
  `sync_registry_metadata()` was reduced to a read-only drift check, and
  `ticker_metadata` was removed from its `MARKET_TABLE_MIGRATION_COLUMNS` map, so no
  second schema manager patches that table. Don't reintroduce either.
- Ingest is **plan-only by default**; `--apply` writes. Quarantined issues are never
  merged silently.
- Merge key is **ISIN**, never the ticker. An existing row's ticker changes only via
  `rename-ticker` (which relies on `ON UPDATE CASCADE` across the six FK tables —
  `prices`, `fundamentals`, `features`, `alt_data`, `insider_transactions`,
  `option_metrics`). Hand-editing a ticker in the CSV orphans its history.
- **Currency is never auto-refreshed.** Upload files carry `DenCurrency`, a
  *denomination* currency that is frequently wrong about the trading currency
  (EUR for LSE names, USD for Hong Kong lines). Mismatches are reported, not applied.
- `scope_status` is the single **coverage** dial: `active` (collected, features
  computed, default training/dashboard scope), `inactive` (collected only),
  `watchlist` (metadata only, no automatic backfill). It does **not** govern
  email delivery — see the email-focus workflow below.
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
`RESEND_API_KEY`, `RESEND_FROM`, `EMAIL_TO`, `EMAIL_USER`, `EMAIL_PASS`.

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
