-- =============================================================================
-- DB1: market — add scope_status + scope_group columns to ticker_metadata
-- Target: Aiven Postgres 16+
--
-- REWRITTEN 2026-07-29: the original file used `ADD CONSTRAINT IF NOT EXISTS`,
-- which is not valid Postgres — the migration errored and was therefore NEVER
-- applied anywhere (verified against the live DB via information_schema before
-- this rewrite: the live columns were created independently by MSARN's
-- data/database.py column-migration map, and the live `active` boolean was
-- stale). Rewriting an unapplied migration is safe; this version is guarded so
-- it is correct both on a fresh database (0001-only schema) and on the live
-- database (scope columns already present with meaningful data).
--
-- What this migration does:
--   1. Adds scope_status TEXT (nullable here; 0003 makes it NOT NULL).
--   2. Backfills scope_status ONLY where it is NULL, from the legacy `active`
--      boolean when that column still exists. Rows with existing scope_status
--      values are never touched — on the live DB those values are the truth
--      and `active` is stale.
--   3. Adds the three scope-group audience flags.
--   4. Drops the legacy `active` column (guarded; verified unreferenced in
--      MSARN, Companies_News, and research_commons SQL).
--   5. Adds the allowed-values CHECK constraint via the pg_constraint guard
--      pattern (see migrations/news/0005_schema_precision.sql).
-- =============================================================================

BEGIN;

-- Step 1: add scope_status (no default: a blanket 'active' default would have
-- silently opted every pre-existing row into production pipelines).
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_status TEXT;

-- Step 2: backfill only NULL rows, only while the legacy boolean still exists.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'ticker_metadata'
          AND column_name = 'active'
    ) THEN
        UPDATE ticker_metadata
        SET scope_status = CASE WHEN active THEN 'active' ELSE 'inactive' END
        WHERE scope_status IS NULL;
    END IF;
END $$;

-- Step 3: scope group flags (SMALLINT; 0003 normalizes any pre-existing
-- INTEGER variants created by the legacy MSARN column-migration map).
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_pm_ch     SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_pm_de     SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_advisory  SMALLINT NOT NULL DEFAULT 0;

-- Step 4: drop the legacy boolean.
ALTER TABLE ticker_metadata DROP COLUMN IF EXISTS active;

-- Step 5: allowed-values CHECK (NULLs pass a CHECK; 0003 adds NOT NULL).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_ticker_metadata_scope_status'
    ) THEN
        ALTER TABLE ticker_metadata
            ADD CONSTRAINT chk_ticker_metadata_scope_status
            CHECK (scope_status IN ('active', 'inactive', 'watchlist'));
    END IF;
END $$;

COMMIT;
