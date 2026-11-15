-- =============================================================================
-- DB1: market — allow scope_status = 'delisted'
-- Target: Aiven Postgres 16+
--
-- research_commons.scope_policy gained a fourth tier on 2026-11-15: 'delisted'
-- runs no pipeline stage at all. The canonical row stays so its ISIN is still
-- known (a later universe upload merges onto it instead of re-adding the name
-- as watchlist). The CHECK from 0002 only allowed active/inactive/watchlist,
-- so sync-db would fail on the first delisted row.
--
-- Idempotent: the constraint is dropped and re-created with the full list on
-- every run (DROP CONSTRAINT IF EXISTS is valid Postgres; ADD CONSTRAINT IF NOT
-- EXISTS is not — see 0002's header).
-- =============================================================================

BEGIN;

ALTER TABLE ticker_metadata DROP CONSTRAINT IF EXISTS chk_ticker_metadata_scope_status;
ALTER TABLE ticker_metadata
    ADD CONSTRAINT chk_ticker_metadata_scope_status
    CHECK (scope_status IN ('active', 'inactive', 'watchlist', 'delisted'));

COMMIT;
