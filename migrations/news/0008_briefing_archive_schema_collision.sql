-- =============================================================================
-- DB2: news — repair the briefing_email_archive schema collision
--
-- Same failure mode as 0007, third table. 0002_compat.sql creates
-- briefing_email_archive with (tickers TEXT[], text_summary, metadata) and no
-- `stats` / `briefing_text`. Companies_News' ensure_briefing_email_archive_table()
-- declares the same name with (tickers JSONB, stats JSONB, briefing_text TEXT).
-- Both use CREATE TABLE IF NOT EXISTS, so this migration won and the consumer's
-- CREATE silently no-opped; its INSERT then names two columns that do not
-- exist and is swallowed by a `except Exception: logger.warning` in main.py.
--
-- Live effect: nothing has been archived since 2026-04-06 — including the
-- 2026-08-27 send, which reached recipients but left no row. The damage is not
-- just audit history: fetch_recent_digest_ticker_history() reads
-- featured_tickers from this table to build the rotation/diversity nudge, so
-- with no rows it finds sends_found = 0, every appearance ratio pins to the
-- neutral 0.5, and the "don't feature the same companies every week" logic has
-- been inert for five months.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, plus a guarded type change that is a
-- no-op once tickers is already jsonb.
-- =============================================================================

BEGIN;

ALTER TABLE briefing_email_archive ADD COLUMN IF NOT EXISTS stats            JSONB;
ALTER TABLE briefing_email_archive ADD COLUMN IF NOT EXISTS briefing_text    TEXT;
ALTER TABLE briefing_email_archive ADD COLUMN IF NOT EXISTS featured_tickers JSONB;

-- tickers: TEXT[] here, JSONB in the consumer that writes it. to_jsonb()
-- preserves the existing rows rather than dropping five months of history.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'briefing_email_archive'
          AND column_name = 'tickers'
          AND data_type = 'ARRAY'
    ) THEN
        ALTER TABLE briefing_email_archive
            ALTER COLUMN tickers TYPE JSONB USING to_jsonb(tickers);
    END IF;
END $$;

COMMIT;
