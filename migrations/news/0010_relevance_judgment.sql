-- =============================================================================
-- DB2: news — judged (article, ticker) relevance + relevance-weighted aggregate
-- Spec: docs/spec-jev-relevance-gate.md. Additive only.
--
-- article_companies.relevance keeps the string-match prior written at sync
-- time. The judge writes next to it:
--   relevance_judged     p(article is materially about this company), 0..1;
--                        NULL = unjudged, the prior stays in effect
--   relevance_judge      judge version that produced relevance_judged; set
--                        only together with it
--   relevance_judged_at  last judge attempt. A failed attempt sets this alone
--                        (judged and judge stay NULL) so the link is retried a
--                        day later instead of re-sent every run.
-- Effective relevance = COALESCE(relevance_judged, relevance) — read it through
-- research_commons.db_news.reads, don't restate it.
--
-- sentiment_daily gains relevance-weighted columns. The existing columns keep
-- today's definition, so consumers opt in rather than having their inputs
-- change underneath them via @main.
-- =============================================================================

BEGIN;

ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS relevance_judged DOUBLE PRECISION;
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS relevance_judge TEXT;
ALTER TABLE article_companies ADD COLUMN IF NOT EXISTS relevance_judged_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'article_companies_relevance_judged_check'
    ) THEN
        ALTER TABLE article_companies
            ADD CONSTRAINT article_companies_relevance_judged_check
            CHECK (relevance_judged IS NULL OR relevance_judged BETWEEN 0 AND 1);
    END IF;
END $$;

-- Pending-links scan in judge_pending_links().
CREATE INDEX IF NOT EXISTS idx_article_companies_unjudged
    ON article_companies (article_id)
    WHERE relevance_judged IS NULL;

-- Daily call cap: counts attempts made today.
CREATE INDEX IF NOT EXISTS idx_article_companies_judged_at
    ON article_companies (relevance_judged_at)
    WHERE relevance_judged_at IS NOT NULL;

ALTER TABLE sentiment_daily ADD COLUMN IF NOT EXISTS n_articles_relevant INTEGER;
ALTER TABLE sentiment_daily ADD COLUMN IF NOT EXISTS sentiment_mean_weighted DOUBLE PRECISION;
ALTER TABLE sentiment_daily ADD COLUMN IF NOT EXISTS severity_max_relevant INTEGER;
ALTER TABLE sentiment_daily ADD COLUMN IF NOT EXISTS relevance_judge TEXT;

COMMIT;
