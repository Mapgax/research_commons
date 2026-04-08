# research_commons

Shared Python library for the **MSARN / Companies_News / Idee_Scraping** ecosystem.

This package is the *single source of truth* for:

1. Connections to the two production databases:
   - **DB1 — `market`**: prices, fundamentals, features, model registry, backtests.
   - **DB2 — `news`**: raw articles, classifications, sentiment_daily, briefings, themes.
2. The canonical ticker registry and currency map.
3. The unified `LLMClient` (Anthropic Claude primary, Gemini fallback).
4. Shared source-of-news registry and URL/text normalization utilities.
5. Independent homepage source-health monitoring and weekly reports.

> See `../ARCHITECTURE_REFACTOR.md` for the full design document, schema, and
> migration plan. This README is a quick reference only.

## Installation (local, editable)

```bash
cd research_commons
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

The other three projects then import from it:

```python
from research_commons.db_market.reads import load_prices, load_features
from research_commons.db_news.writes  import upsert_article
from research_commons.llm.client      import LLMClient
```

## Environment variables

| Variable | Required | Notes |
| --- | :---: | --- |
| `MARKET_DATABASE_URL` | yes | Aiven Postgres URL for DB1 (`postgres://…/market`) |
| `NEWS_DATABASE_URL` | yes | Aiven Postgres URL for DB2 (`postgres://…/news`) |
| `ANTHROPIC_API_KEY` | yes | Claude (primary LLM) |
| `GEMINI_API_KEY` | no | Fallback LLM if Anthropic fails |
| `LLM_PRIMARY_MODEL` | no | Default `claude-haiku-4-5-20251001` |
| `LLM_FALLBACK_MODEL` | no | Default `gemini-2.5-flash` |
| `RC_LOG_LEVEL` | no | Default `INFO` |
| `SOURCE_HEALTH_TIMEOUT_SEC` | no | Homepage timeout in seconds (default `8`) |
| `SOURCE_HEALTH_MAX_RETRIES` | no | Additional retries after the first attempt (default `2`) |
| `SOURCE_HEALTH_USER_AGENT` | no | User-Agent used for direct homepage checks |
| `SOURCE_HEALTH_REPORT_DIR` | no | Report output directory (`reports/source_health`) |
| `SOURCE_HEALTH_KEYWORDS` | no | Extra comma-separated keyword hints |
| `RESEND_API_KEY` | no | Optional email delivery for source-health reports |
| `RESEND_FROM` | no | Sender address for source-health report emails |
| `EMAIL_TO` | no | Comma-separated report recipients |

Security / external data flows:

- Every prompt sent through `LLMClient` is transmitted to **Anthropic** and
  optionally to **Google (Gemini)**. Treat all article text passed to it as
  leaving your machine.
- Both `MARKET_DATABASE_URL` and `NEWS_DATABASE_URL` connect to **Aiven**
  (managed Postgres in the cloud). Anything you `INSERT` is stored there, not
  on your laptop.
- The source-health monitor sends lightweight requests to third-party
  homepages and `robots.txt` endpoints. It does not reuse scraper logic or
  upload scraped article content.
- If `RESEND_API_KEY` is configured, the generated report is sent to
  **resend.com** for delivery.

## Smoke test

After setting the env vars:

```bash
python3 scripts/check_connections.py
```

This pings both databases, lists the tables it finds, and runs a 1-token LLM
call to verify the API key without writing anything.

## Source health runner

After applying `migrations/news/0003_source_health.sql`, run:

```bash
python3 -m research_commons.source_health --skip-email
```

What it does:

1. Loads homepage URLs from the shared `sources` table (`url` first, otherwise
   `base_url`).
2. Checks `robots.txt`, then performs a lightweight direct homepage `GET`.
3. Appends rows into the isolated `source_health` table.
4. Writes a markdown report to `reports/source_health/`.
5. Optionally emails that report via Resend if `RESEND_*` and `EMAIL_TO` are set.

## Layout

```text
research_commons/
├── __init__.py          # __version__
├── config.py            # env-var loaders + Settings dataclasses
├── tickers.py           # canonical ticker registry, ISO currency map
├── types.py             # shared TypedDicts / pydantic models
├── llm/
│   ├── client.py        # LLMClient (Claude -> Gemini fallback)
│   └── prompts.py       # named prompt templates
├── db_market/
│   ├── connection.py    # ThreadedConnectionPool for DB1
│   ├── reads.py         # load_prices, load_features, ...
│   ├── writes.py        # upsert_prices, upsert_features, ...
│   └── ddl.py           # Python view of expected schema (for tests)
├── db_news/
│   ├── connection.py    # ThreadedConnectionPool for DB2
│   ├── reads.py         # load_articles, load_sentiment_daily, ...
│   ├── writes.py        # upsert_article, upsert_classification, ...
│   └── ddl.py
├── sources/
│   ├── registry.py      # known news source metadata
│   └── normalize.py     # URL canonicalization, content_hash
└── source_health/
    ├── checker.py       # direct homepage checks
    ├── classifier.py    # WORKING / BLOCKED / ERROR mapping
    ├── emailer.py       # optional Resend email delivery
    └── report.py        # DB writes + markdown report generation
```

The actual SQL DDL lives in `migrations/market/0001_init.sql`,
`migrations/news/0001_init.sql`, and `migrations/news/0003_source_health.sql`.
Run them once against the freshly created Aiven databases.

## Status

`research_commons` is still a shared skeleton in many areas. The new
`source_health` package is intentionally independent from those unfinished
write/read helpers so it can run safely without refactoring existing pipelines.
