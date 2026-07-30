#!/usr/bin/env bash
# Double-click this file in Finder to launch the ticker coverage/freshness
# Streamlit report (reports/ticker_universe_report.py). Equivalent to:
#   set -a && source .env && set +a && streamlit run reports/ticker_universe_report.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

streamlit run reports/ticker_universe_report.py
