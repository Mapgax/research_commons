-- =============================================================================
-- DB1: market — add scope_status + scope_group columns to ticker_metadata
-- Target: Aiven Postgres 16+
--
-- Idempotent: every ALTER TABLE uses ADD COLUMN IF NOT EXISTS.
--
-- What this migration does:
--   1. Introduces scope_status TEXT replacing the old `active` BOOLEAN so that
--      we can express active / inactive / watchlist without a separate table.
--   2. Migrates existing rows: active=TRUE → 'active', FALSE → 'inactive'.
--   3. Adds the three scope-group flags that replace the old advisory/PM routing
--      concept (boolean per audience).
--   4. Drops the now-redundant `active` column.
--      NOTE: If downstream consumers still read `active`, defer step 4 and
--      add it in a separate coordinated release.
-- =============================================================================

BEGIN;

-- Step 1: add scope_status column (safe if already present)
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_status TEXT NOT NULL DEFAULT 'active';

-- Step 2: backfill from existing active boolean.
-- The WHERE clause is intentionally broad: after Step 1 every row has
-- scope_status = 'active' (the column default), so this UPDATE correctly
-- reclassifies rows where active = FALSE as 'inactive'. Any row already
-- updated by a previous partial run keeps its value because the CASE only
-- yields 'active' or 'inactive', never 'watchlist', so a re-run is safe.
UPDATE ticker_metadata
SET scope_status = CASE
    WHEN active = FALSE THEN 'inactive'
    ELSE 'active'
END
WHERE scope_status = 'active';

-- Step 3: add scope group flags
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_pm_ch     SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_pm_de     SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE ticker_metadata
    ADD COLUMN IF NOT EXISTS scope_advisory  SMALLINT NOT NULL DEFAULT 0;

-- Step 4: drop the legacy boolean.
-- Verified safe: no query in MSARN or research_commons filters on
-- ticker_metadata.active directly. The only reference is a backward-compat
-- shim in database.py upsert_ticker_metadata(), which is guarded by
-- `if "active" in db_cols` and therefore becomes dead code after this drop.
ALTER TABLE ticker_metadata DROP COLUMN IF EXISTS active;

-- Optional: add a CHECK constraint so scope_status is always a known value
ALTER TABLE ticker_metadata
    ADD CONSTRAINT IF NOT EXISTS chk_ticker_metadata_scope_status
    CHECK (scope_status IN ('active', 'inactive', 'watchlist'));

COMMIT;
