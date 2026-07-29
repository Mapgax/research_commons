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
- `scope_status` is the single coverage/delivery dial: `active` (collected +
  delivered), `inactive` (collected only), `watchlist` (metadata only). The old
  `scope_pm_ch` / `scope_pm_de` / `scope_advisory` audience flags were retired on
  2026-07-29 (values archived in `data/archive/retired_scope_flags_20260729.csv`).
- The CLI refuses to run from an installed copy — `data/` and `reports/` are repo-root
  artifacts, deliberately outside the package.

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
