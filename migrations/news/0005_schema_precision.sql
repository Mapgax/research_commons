-- =============================================================================
-- DB2: news — sentiment provenance + extraction audit
-- Additive only: every change is ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
-- Covers:
--   J12 source provenance on sentiment_daily (source_count, source_tiers_present)
--   J16 extraction audit on article_companies (extraction_model, low_confidence)
--   H3  article quality score on article_classifications
--
-- J10 (articles.published_at type fix) was dropped from this migration: the
-- column is already TIMESTAMPTZ in both 0001_init.sql and investment_radar's
-- legacy db/schema.sql, so there is no text-cast workaround to migrate away
-- from. The real J10 gap was naive (timezone-less) datetimes reaching that
-- column from RSS feeds without an explicit offset — fixed in
-- investment_radar/scrapers/rss_scraper.py (_ensure_utc), not here.
-- =============================================================================

BEGIN;

-- J12: sentiment provenance
ALTER TABLE sentiment_daily ADD COLUMN IF NOT EXISTS source_count INTEGER;
ALTER TABLE sentiment_daily ADD COLUMN IF NOT EXISTS source_tiers_present TEXT[];

-- J16: extraction audit
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS extraction_model TEXT;
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS low_confidence BOOLEAN DEFAULT FALSE;

-- H3: article quality score (article_classifications already carries the
-- investment_radar compat columns added in 0002_compat.sql)
ALTER TABLE article_classifications ADD COLUMN IF NOT EXISTS content_quality_score DOUBLE PRECISION;
ALTER TABLE article_classifications ADD COLUMN IF NOT EXISTS source_tier TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'article_classifications_content_quality_score_check'
    ) THEN
        ALTER TABLE article_classifications
            ADD CONSTRAINT article_classifications_content_quality_score_check
            CHECK (content_quality_score IS NULL OR content_quality_score BETWEEN 0 AND 1);
    END IF;
END $$;

COMMIT;
