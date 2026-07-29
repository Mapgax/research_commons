-- =============================================================================
-- DB1: market — retire the audience-routing flags on ticker_metadata
-- Target: Aiven Postgres 16+
--
-- scope_pm_ch / scope_pm_de / scope_advisory encoded which audience a name was
-- delivered to. Only scope_pm_ch was ever wired into a runtime path (the
-- Companies_News briefing email); the other two were carried but never read.
-- Coverage and delivery now run off the single scope_status dial:
--
--     active    → collected AND delivered (focus list)
--     inactive  → collected only
--     watchlist → metadata only
--
-- The flag values as of the drop are archived in
-- data/archive/retired_scope_flags_20260729.csv (and in this repo's git
-- history), so the routing sets can be reconstructed if they are ever needed.
--
-- Idempotent: DROP COLUMN IF EXISTS.
-- =============================================================================

BEGIN;

ALTER TABLE ticker_metadata DROP COLUMN IF EXISTS scope_pm_ch;
ALTER TABLE ticker_metadata DROP COLUMN IF EXISTS scope_pm_de;
ALTER TABLE ticker_metadata DROP COLUMN IF EXISTS scope_advisory;

COMMIT;
