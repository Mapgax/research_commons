-- =============================================================================
-- DB1: market — consolidate ticker_metadata into the canonical registry shape
-- Target: Aiven Postgres 16+
--
-- Written against a live-schema introspection (2026-07-29). The live table had
-- accumulated drift from a second schema manager (MSARN data/database.py
-- MARKET_TABLE_MIGRATION_COLUMNS, retired in the same coordinated release as
-- this migration):
--   * both `sector` (0001, 415/459 NULL) and `sector_name` (MSARN, populated)
--   * `updated_at` created as TEXT holding ISO-8601 strings
--   * scope flags created as nullable INTEGER instead of SMALLINT NOT NULL
--   * `scope_status` nullable, NULL on two placeholder rows (TSLA/XOM:
--     no ISIN, company_name = ticker)
--
-- After this migration the table matches research_commons.tickers.TickerInfo:
-- sector_name is the only sector column, updated_at is TIMESTAMPTZ NOT NULL,
-- scope_status is NOT NULL DEFAULT 'watchlist' (fail-safe: a new row never
-- silently enters production pipelines), scope flags are SMALLINT NOT NULL.
--
-- Idempotent: every step is guarded or a no-op on re-run.
-- =============================================================================

BEGIN;

-- Step 1: placeholder rows (no ISIN, name equals ticker) are metadata-only.
-- No scope_status guard: 0002's backfill may have just promoted them to
-- 'active' from the stale legacy boolean.
UPDATE ticker_metadata
SET scope_status = 'watchlist'
WHERE isin IS NULL
  AND company_name = ticker;

-- Step 2: consolidate sector -> sector_name, drop the 0001 column.
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS sector_name TEXT;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'ticker_metadata'
          AND column_name = 'sector'
    ) THEN
        UPDATE ticker_metadata
        SET sector_name = sector
        WHERE sector_name IS NULL AND sector IS NOT NULL;

        ALTER TABLE ticker_metadata DROP COLUMN sector;
    END IF;
END $$;

-- Step 3: updated_at — convert the legacy TEXT column, or create it fresh.
DO $$
DECLARE
    v_type TEXT;
BEGIN
    SELECT data_type INTO v_type
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'ticker_metadata'
      AND column_name = 'updated_at';

    IF v_type IS NULL THEN
        ALTER TABLE ticker_metadata ADD COLUMN updated_at TIMESTAMPTZ;
    ELSIF v_type = 'text' THEN
        ALTER TABLE ticker_metadata
            ALTER COLUMN updated_at TYPE TIMESTAMPTZ
            USING NULLIF(updated_at, '')::timestamptz;
    END IF;
END $$;

UPDATE ticker_metadata SET updated_at = added_at WHERE updated_at IS NULL;
ALTER TABLE ticker_metadata ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE ticker_metadata ALTER COLUMN updated_at SET NOT NULL;

-- Step 4: scope_status becomes NOT NULL with the fail-safe default.
UPDATE ticker_metadata SET scope_status = 'watchlist' WHERE scope_status IS NULL;
ALTER TABLE ticker_metadata ALTER COLUMN scope_status SET DEFAULT 'watchlist';
ALTER TABLE ticker_metadata ALTER COLUMN scope_status SET NOT NULL;

-- Step 5: normalize scope flags (legacy rows: nullable INTEGER).
UPDATE ticker_metadata SET scope_pm_ch    = 0 WHERE scope_pm_ch    IS NULL;
UPDATE ticker_metadata SET scope_pm_de    = 0 WHERE scope_pm_de    IS NULL;
UPDATE ticker_metadata SET scope_advisory = 0 WHERE scope_advisory IS NULL;

ALTER TABLE ticker_metadata ALTER COLUMN scope_pm_ch    TYPE SMALLINT;
ALTER TABLE ticker_metadata ALTER COLUMN scope_pm_de    TYPE SMALLINT;
ALTER TABLE ticker_metadata ALTER COLUMN scope_advisory TYPE SMALLINT;

ALTER TABLE ticker_metadata ALTER COLUMN scope_pm_ch    SET NOT NULL;
ALTER TABLE ticker_metadata ALTER COLUMN scope_pm_de    SET NOT NULL;
ALTER TABLE ticker_metadata ALTER COLUMN scope_advisory SET NOT NULL;

ALTER TABLE ticker_metadata ALTER COLUMN scope_pm_ch    SET DEFAULT 0;
ALTER TABLE ticker_metadata ALTER COLUMN scope_pm_de    SET DEFAULT 0;
ALTER TABLE ticker_metadata ALTER COLUMN scope_advisory SET DEFAULT 0;

COMMIT;
