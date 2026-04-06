#!/usr/bin/env python3
"""Smoke-test for research_commons.

Run AFTER setting MARKET_DATABASE_URL, NEWS_DATABASE_URL and ANTHROPIC_API_KEY
in your shell or .env file.

What it does (read-only — no writes):

  1. Build the Settings object (validates env vars exist).
  2. Open and immediately return one connection to DB1 and DB2.
  3. List the user tables in each DB and compare against the EXPECTED_TABLES
     constants in db_market.ddl / db_news.ddl.
  4. Send a 1-token "ping" prompt through LLMClient to verify the API key.
  5. Print a summary table.

Privacy ⚠️
Step 4 transmits the literal string "ping" to Anthropic. No customer data
is sent. If you don't want any external call at all, pass --no-llm.

Usage:
    python3 scripts/check_connections.py
    python3 scripts/check_connections.py --no-llm
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Iterable

logger = logging.getLogger("check_connections")


def _check_db(label: str, get_connection_fn, expected: Iterable[str]) -> bool:
    """Open one pooled connection and compare table list against `expected`."""
    expected_set = set(expected)
    print(f"\n[{label}] connecting…")
    try:
        with get_connection_fn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
                found = {row[0] for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] ❌ connection failed: {exc}")
        return False

    missing = expected_set - found
    extra = found - expected_set
    print(f"[{label}] ✅ connected. {len(found)} tables found.")
    if missing:
        print(f"[{label}] ⚠️  missing tables (run the migration): {sorted(missing)}")
    if extra:
        print(f"[{label}] ℹ️  extra tables not in DDL view: {sorted(extra)}")
    return not missing


def _check_llm() -> bool:
    print("\n[llm] sending 1-token 'ping' to Anthropic …")
    try:
        from research_commons.llm.client import LLMClient
        client = LLMClient()
        result = client.generate("ping", response_format="text", max_output_tokens=8)
    except NotImplementedError:
        print("[llm] ⏭  LLMClient is still a stub — skipping.")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[llm] ❌ failed: {exc}")
        return False
    print(f"[llm] ✅ ok ({result.provider} / {result.model_used}, "
          f"{result.input_tokens}+{result.output_tokens} tokens)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip the LLM ping (no external API call).")
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Lazy imports so a missing env var produces a friendly error from get_settings()
    # rather than a stack trace from psycopg2.
    try:
        from research_commons.config import get_settings
        from research_commons.db_market import get_connection as get_market_conn
        from research_commons.db_market.ddl import EXPECTED_TABLES as MARKET_TABLES
        from research_commons.db_news import get_connection as get_news_conn
        from research_commons.db_news.ddl import EXPECTED_TABLES as NEWS_TABLES
    except Exception as exc:  # noqa: BLE001
        print(f"❌ import failed: {exc}")
        return 2

    try:
        settings = get_settings()
    except NotImplementedError:
        print("⏭  config.get_settings() is still a stub — implement it first.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}")
        return 2

    print(f"using LLM primary={settings.llm_primary_model} fallback={settings.llm_fallback_model}")

    ok_market = _check_db("market", get_market_conn, MARKET_TABLES)
    ok_news   = _check_db("news",   get_news_conn,   NEWS_TABLES)
    ok_llm    = True if args.no_llm else _check_llm()

    print("\n" + "=" * 50)
    print(f"market: {'OK' if ok_market else 'FAIL'}")
    print(f"news:   {'OK' if ok_news else 'FAIL'}")
    print(f"llm:    {'OK' if ok_llm else 'FAIL' if not args.no_llm else 'SKIPPED'}")
    print("=" * 50)
    return 0 if (ok_market and ok_news and ok_llm) else 1


if __name__ == "__main__":
    sys.exit(main())
