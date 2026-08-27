-- =============================================================================
-- DB2: news — repair the manual_research_* schema collision
--
-- 0002_compat.sql created manual_research_documents / manual_research_items
-- with a schema invented for that migration (item_type/content/source_snippet),
-- while Companies_News' own ensure_manual_research_tables() creates the same two
-- names with the schema its code actually reads and writes (kind/topic/
-- event_type/summary/calibrated_score, source_path/document_type/received_at_ts).
-- Both sides use CREATE TABLE IF NOT EXISTS, so whichever ran first won — the
-- migration did, the consumer's CREATE silently no-opped, and its ADD COLUMN
-- repair loop could not close the gap because it omitted `kind`, `source_path`
-- and `document_type`.
--
-- Live symptom (2026-08-27 briefing): "column i.topic does not exist" on both
-- fetch_manual_research_for_tickers() and fetch_manual_macro_items(), so the
-- briefing silently shipped without any manually curated research. Writes were
-- broken too — manual_research_items.content is NOT NULL with no default and
-- the consumer's INSERT never supplies it.
--
-- This is the same failure mode CLAUDE.md already warns about for
-- ticker_metadata: two schema managers for one table. 0002_compat.sql's blocks
-- are now aligned to the consumer's schema so the two definitions agree; this
-- file repairs databases already carrying the wrong shape.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS / DROP NOT NULL are both re-runnable.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. manual_research_documents — columns the consumer writes and reads
-- ---------------------------------------------------------------------------
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS source_path       TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS document_type     TEXT NOT NULL DEFAULT 'file';
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS message_id        TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS message_key       TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS received_at_ts    TIMESTAMPTZ;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS body_text         TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS attachment_text   TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS combined_text     TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS llm_summary       TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS llm_model         TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS error_message     TEXT;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS retry_count       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS source_channel    TEXT NOT NULL DEFAULT 'local_file';
ALTER TABLE manual_research_documents ADD COLUMN IF NOT EXISTS raw_metadata      JSONB;

-- 0002's source_type is NOT NULL with a default, so consumer inserts that omit
-- it still work; leave it rather than churn a column something may yet read.

-- ---------------------------------------------------------------------------
-- 2. manual_research_items — columns the consumer writes and reads
-- ---------------------------------------------------------------------------
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS kind             TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS company_name     TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS topic            TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS event_type       TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS importance_score DOUBLE PRECISION;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS source_weight    DOUBLE PRECISION DEFAULT 0.6;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS calibrated_score DOUBLE PRECISION;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS item_hash        TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS summary          TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS evidence         TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS time_horizon     TEXT;
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE manual_research_items ADD COLUMN IF NOT EXISTS raw_item         JSONB;

-- 0002 made `ticker` and `content` NOT NULL. The consumer stores macro items
-- (ticker IS NULL by definition) and never writes `content` at all — it writes
-- `summary`/`evidence` — so both constraints reject every insert it makes.
ALTER TABLE manual_research_items ALTER COLUMN ticker  DROP NOT NULL;
ALTER TABLE manual_research_items ALTER COLUMN content DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_manres_items_kind ON manual_research_items (kind);

COMMIT;
