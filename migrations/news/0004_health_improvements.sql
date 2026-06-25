-- =============================================================================
-- DB2: news — source-health observability + reliability tiering
-- Additive only: every change is ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
-- Covers:
--   J6  consecutive-failure tracking on source_health
--   J7  reliability_tier / ingestion_method on sources (kept separate from
--       the pre-existing `tier` column, which already has two live
--       vocabularies — see architecture review 2026-04-13)
--   J9  circuit breaker + freshness tracking on sources
-- =============================================================================

BEGIN;

-- J6: consecutive failure tracking
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS failure_reason TEXT;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS failure_detail TEXT;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS articles_found INTEGER;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS suggested_action TEXT;

-- J7: source reliability tiering (new column — `tier` already carries two
-- other vocabularies from research_commons and investment_radar)
ALTER TABLE sources ADD COLUMN IF NOT EXISTS reliability_tier TEXT DEFAULT 'secondary';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS ingestion_method TEXT DEFAULT 'rss';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sources_reliability_tier_check'
    ) THEN
        ALTER TABLE sources
            ADD CONSTRAINT sources_reliability_tier_check
            CHECK (reliability_tier IN ('core', 'secondary', 'monitor'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sources_ingestion_method_check'
    ) THEN
        ALTER TABLE sources
            ADD CONSTRAINT sources_ingestion_method_check
            CHECK (ingestion_method IN ('rss', 'static_html', 'api', 'sitemap', 'manual'));
    END IF;
END $$;

-- J9: circuit breaker + freshness
ALTER TABLE sources ADD COLUMN IF NOT EXISTS circuit_open BOOLEAN DEFAULT FALSE;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_ok_at TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS stale_threshold_days INTEGER DEFAULT 14;

COMMIT;
