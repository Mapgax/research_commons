# research_commons

Shared Python library for the **MSARN / Companies_News / Idee_Scraping** ecosystem.

This package is the *single source of truth* for:

1. Connections to the two production databases:
   - **DB1 — `market`**: prices, fundamentals, features, model registry, backtests.
   - **DB2 — `news`**: raw articles, classifications, sentiment_daily, briefings, themes.
2. The canonical ticker registry and currency map.
3. The unified `LLMClient` (Anthropic Claude primary, Gemini fallback).
4. Shared source-of-news registry and URL/text normalization utilities.

> See `../ARCHITECTURE_REFACTOR.md` for the full design document, schema, and
> migration plan. This README is a *quick reference* only.

## Installation (local, editable)

```bash
cd proposed_research_commons
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The other three projects then import from it:

```python
from research_commons.db_market.reads import load_prices, load_features
from research_commons.db_news.writes  import upsert_article
from research_commons.llm.client      import LLMClient
```

## Environment variables

| Variable               | Required | Notes                                                      |
| ---------------------- | :------: | ---------------------------------------------------------- |
| `MARKET_DATABASE_URL`  |  yes     | Aiven Postgres URL for DB1 (`postgres://…/market`)         |
| `NEWS_DATABASE_URL`    |  yes     | Aiven Postgres URL for DB2 (`postgres://…/news`)           |
| `ANTHROPIC_API_KEY`    |  yes     | Claude (primary LLM)                                       |
| `GEMINI_API_KEY`       |  no      | Fallback LLM if Anthropic fails                            |
| `LLM_PRIMARY_MODEL`    |  no      | Default `claude-haiku-4-5-20251001`                        |
| `LLM_FALLBACK_MODEL`   |  no      | Default `gemini-2.5-flash`                                 |
| `RC_LOG_LEVEL`         |  no      | Default `INFO`                                             |

⚠️ **External data flows** (be aware before running):

- Every prompt sent through `LLMClient` is transmitted to **Anthropic** and
  optionally to **Google (Gemini)**. Treat all article text passed to it as
  leaving your machine.
- Both `MARKET_DATABASE_URL` and `NEWS_DATABASE_URL` connect to **Aiven**
  (managed Postgres in the cloud). Anything you `INSERT` is stored there, not
  on your laptop.

## Smoke test

After setting the env vars:

```bash
python3 scripts/check_connections.py
```

This pings both databases, lists the tables it finds, and runs a 1-token LLM
call to verify the API key — without writing anything.

## Layout

```
research_commons/
├── __init__.py          # __version__
├── config.py            # env-var loader + Settings dataclass
├── tickers.py           # canonical ticker registry, ISO currency map
├── types.py             # shared TypedDicts / pydantic models
├── llm/
│   ├── client.py        # LLMClient (Claude → Gemini fallback)
│   └── prompts.py       # named prompt templates
├── db_market/
│   ├── connection.py    # ThreadedConnectionPool for DB1
│   ├── reads.py         # load_prices, load_features, …
│   ├── writes.py        # upsert_prices, upsert_features, …
│   └── ddl.py           # Python view of expected schema (for tests)
├── db_news/
│   ├── connection.py    # ThreadedConnectionPool for DB2
│   ├── reads.py         # load_articles, load_sentiment_daily, …
│   ├── writes.py        # upsert_article, upsert_classification, …
│   └── ddl.py
└── sources/
    ├── registry.py      # known news source metadata
    └── normalize.py     # URL canonicalization, content_hash
```

The actual SQL DDL lives in `migrations/market/0001_init.sql` and
`migrations/news/0001_init.sql`. Run them once against the freshly created
Aiven databases.

## Status

Skeleton only — every function currently raises `NotImplementedError`.
The signatures are **frozen** (see §8.1 of `ARCHITECTURE_REFACTOR.md`); fill in
bodies in dependency order: `connection → writes → reads → llm → sources`.
