-- =============================================================================
-- DB2: news — ticker alias resolution (J21)
-- Deterministic company-name -> ticker lookup that supplements (does not
-- replace) the LLM-based extraction in investment_radar's company_mapper.
-- Populated opportunistically from high-confidence, explicit LLM mentions —
-- no hand-curated seed data is loaded by this migration.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS ticker_aliases (
    alias       TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    source      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticker_aliases_ticker
    ON ticker_aliases (ticker);

COMMIT;
