-- =============================================================================
-- DB2: news — resync identity sequences after a data-only restore
--
-- On 2026-04-06 this database was restored with rows carrying explicit ids and
-- the owning sequences were never advanced (the classic `pg_dump --data-only`
-- footgun). Every INSERT into an affected table has raised
-- "duplicate key value violates unique constraint ..._pkey" ever since, and in
-- each case the caller swallows it:
--
--   briefing_sent_items       max(id)=11188  last_value=87   <- the sent-item
--       dedup store. Broken since April, so filter_unsent_ticker_data() has
--       had no new history to compare against and the weekly briefing has been
--       re-including articles it already sent.
--   briefing_email_archive    max(id)=22     last_value=1
--   manual_research_documents max(id)=3      last_value=1
--
-- Written as a loop over every serial/identity sequence rather than three
-- setval() calls, so the next restore self-heals on the next migration run
-- instead of failing silently for five months.
--
-- Idempotent: setval to max(id) is a no-op once the sequence is already ahead,
-- and the guard skips sequences whose table is empty.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    rec       RECORD;
    max_id    BIGINT;
    seq_last  BIGINT;
BEGIN
    FOR rec IN
        SELECT c.oid::regclass        AS tbl,
               a.attname              AS col,
               pg_get_serial_sequence(c.oid::regclass::text, a.attname) AS seq
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE c.relkind = 'r'
          AND n.nspname = 'public'
          AND pg_get_serial_sequence(c.oid::regclass::text, a.attname) IS NOT NULL
    LOOP
        EXECUTE format('SELECT max(%I) FROM %s', rec.col, rec.tbl) INTO max_id;
        CONTINUE WHEN max_id IS NULL;

        EXECUTE format('SELECT last_value FROM %s', rec.seq) INTO seq_last;
        CONTINUE WHEN seq_last >= max_id;

        PERFORM setval(rec.seq, max_id);
        RAISE NOTICE 'resynced % -> % (was %)', rec.seq, max_id, seq_last;
    END LOOP;
END $$;

COMMIT;
