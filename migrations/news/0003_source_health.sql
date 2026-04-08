-- =============================================================================
-- DB2: news — source homepage health monitoring
-- Adds an isolated append-only table for direct homepage availability checks.
-- No existing tables are modified.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS source_health (
    id            SERIAL PRIMARY KEY,
    source_url    TEXT NOT NULL,
    status        TEXT NOT NULL,
    http_status   INTEGER,
    response_time DOUBLE PRECISION,
    last_checked  TIMESTAMPTZ NOT NULL,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_health_source_url
    ON source_health (source_url);

CREATE INDEX IF NOT EXISTS idx_source_health_last_checked
    ON source_health (last_checked DESC);

COMMIT;
