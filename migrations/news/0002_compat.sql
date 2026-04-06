-- =============================================================================
-- DB2: news — compatibility patch
-- Adds columns required by Companies_News and Idee_Scraping/investment_radar
-- on top of the canonical schema created in 0001_init.sql.
--
-- Idempotent: every ALTER TABLE uses IF NOT EXISTS.
-- Run AFTER 0001_init.sql.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. sources — extra columns for Idee_Scraping scrapers
--    (investment_radar uses 'url', 'category', 'scraper_type', 'active')
-- ---------------------------------------------------------------------------
ALTER TABLE sources ADD COLUMN IF NOT EXISTS url TEXT;                          -- IR: scraper target URL
ALTER TABLE sources ADD COLUMN IF NOT EXISTS category TEXT;                     -- IR: 'equity_ideas', 'macro_strategy', …
ALTER TABLE sources ADD COLUMN IF NOT EXISTS scraper_type TEXT;                 -- IR: 'static' | 'rss' | 'structured'
ALTER TABLE sources ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- 2. articles — extra columns for investment_radar
--    (IR uses 'url', 'content_snippet', 'full_content', 'scraped_at')
-- ---------------------------------------------------------------------------
ALTER TABLE articles ADD COLUMN IF NOT EXISTS url TEXT;                         -- IR: original (non-canonical) URL
ALTER TABLE articles ADD COLUMN IF NOT EXISTS content_snippet TEXT;             -- IR: short excerpt
ALTER TABLE articles ADD COLUMN IF NOT EXISTS full_content TEXT;                -- IR: full scraped text
ALTER TABLE articles ADD COLUMN IF NOT EXISTS scraped_at TIMESTAMPTZ;          -- IR alias for fetched_at

-- Keep in sync: when a row is inserted via IR code, copy url → canonical_url
-- and fetched_at → scraped_at. Handled by application code in db/client.py.

-- ---------------------------------------------------------------------------
-- 3. article_classifications — extra columns for investment_radar
--    (IR's 'classifications' table uses theme_primary, sentiment TEXT, etc.)
-- ---------------------------------------------------------------------------
ALTER TABLE article_classifications ADD COLUMN IF NOT EXISTS theme_primary TEXT;
ALTER TABLE article_classifications ADD COLUMN IF NOT EXISTS themes_secondary TEXT[];
ALTER TABLE article_classifications ADD COLUMN IF NOT EXISTS relevance_score DOUBLE PRECISION;
ALTER TABLE article_classifications ADD COLUMN IF NOT EXISTS model_used TEXT;
ALTER TABLE article_classifications ADD COLUMN IF NOT EXISTS sentiment TEXT;    -- IR: 'bullish'|'bearish'|'neutral'

-- ---------------------------------------------------------------------------
-- 4. article_companies — extra columns for investment_radar
--    (IR's 'company_mentions' uses company_name, mention_type, confidence)
-- ---------------------------------------------------------------------------
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS company_name TEXT;
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS mention_type TEXT DEFAULT 'explicit';
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();

-- ---------------------------------------------------------------------------
-- 5. pipeline_runs — extra columns for investment_radar's run_log
-- ---------------------------------------------------------------------------
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS run_type TEXT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS sources_scraped INTEGER;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS articles_new INTEGER;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS articles_classified INTEGER;

-- ---------------------------------------------------------------------------
-- 6. Companies_News — tables that were previously in defaultdb
-- ---------------------------------------------------------------------------

-- briefing_sent_items: delivery dedup (from delivery_state.py)
CREATE TABLE IF NOT EXISTS briefing_sent_items (
    id            BIGSERIAL PRIMARY KEY,
    content_hash  TEXT NOT NULL UNIQUE,
    ticker        TEXT NOT NULL,
    source        TEXT NOT NULL,
    title         TEXT,
    url           TEXT,
    published_at  TEXT,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_data      JSONB
);
CREATE INDEX IF NOT EXISTS idx_briefing_sent_ticker  ON briefing_sent_items (ticker);
CREATE INDEX IF NOT EXISTS idx_briefing_sent_sent_at ON briefing_sent_items (sent_at DESC);

-- manual_research_documents: ingested email/PDF/file documents
CREATE TABLE IF NOT EXISTS manual_research_documents (
    id           BIGSERIAL PRIMARY KEY,
    file_hash    TEXT NOT NULL UNIQUE,
    file_name    TEXT NOT NULL,
    source_type  TEXT NOT NULL DEFAULT 'file',  -- 'file' | 'gmail' | 'paste'
    subject      TEXT,
    sender       TEXT,
    received_at  TIMESTAMPTZ,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_text     TEXT,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_manres_docs_processed ON manual_research_documents (processed_at DESC);

-- manual_research_items: structured items extracted from documents
CREATE TABLE IF NOT EXISTS manual_research_items (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT NOT NULL REFERENCES manual_research_documents(id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL,
    item_type       TEXT NOT NULL DEFAULT 'note',
    content         TEXT NOT NULL,
    source_snippet  TEXT,
    sentiment       TEXT,
    published_at    TIMESTAMPTZ,
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_manres_items_ticker ON manual_research_items (ticker);

-- briefing_email_archive: full HTML archive of sent briefings
CREATE TABLE IF NOT EXISTS briefing_email_archive (
    id           BIGSERIAL PRIMARY KEY,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tickers      TEXT[],
    subject      TEXT,
    html_body    TEXT,
    text_summary TEXT,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- pipeline_heartbeat: lightweight liveness record
CREATE TABLE IF NOT EXISTS pipeline_heartbeat (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stage      TEXT NOT NULL,
    status     TEXT NOT NULL,
    details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_heartbeat_ts ON pipeline_heartbeat (ts DESC);

COMMIT;
