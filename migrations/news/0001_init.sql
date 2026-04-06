-- =============================================================================
-- DB2: news — initial schema
-- Target: Aiven Postgres 16+
-- See ARCHITECTURE_REFACTOR.md §4.2 for the design rationale.
-- =============================================================================

BEGIN;

-- ----- Source registry -------------------------------------------------------

CREATE TABLE IF NOT EXISTS sources (
    name              TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL,
    tier              TEXT NOT NULL,
    base_url          TEXT,
    requires_api_key  BOOLEAN NOT NULL DEFAULT FALSE,
    rate_limit_per_min INTEGER,
    notes             TEXT,
    inserted_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----- Articles --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS articles (
    id                BIGSERIAL PRIMARY KEY,
    source            TEXT NOT NULL REFERENCES sources(name),
    source_article_id TEXT,
    url               TEXT NOT NULL,
    canonical_url     TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    title             TEXT NOT NULL,
    body              TEXT,
    published_at      TIMESTAMPTZ,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    language          CHAR(2),
    raw               JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (content_hash)
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source    ON articles (source, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_canonical ON articles (canonical_url);

-- ----- Classifications (versioned) -------------------------------------------

CREATE TABLE IF NOT EXISTS article_classifications (
    article_id          BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    classifier_version  TEXT NOT NULL,
    event_type          TEXT,
    severity            INTEGER,
    sentiment_score     DOUBLE PRECISION,
    summary             TEXT,
    raw                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    classified_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, classifier_version),
    CHECK (severity IS NULL OR severity BETWEEN 1 AND 5),
    CHECK (sentiment_score IS NULL OR sentiment_score BETWEEN -1.0 AND 1.0)
);

-- Many-to-many: which tickers does an article mention?
CREATE TABLE IF NOT EXISTS article_companies (
    article_id BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    ticker     TEXT   NOT NULL,
    relevance  DOUBLE PRECISION,    -- optional 0..1
    PRIMARY KEY (article_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_article_companies_ticker
    ON article_companies (ticker);

-- ----- Daily aggregates (the bridge to DB1.features) -------------------------

CREATE TABLE IF NOT EXISTS sentiment_daily (
    ticker          TEXT NOT NULL,
    date            DATE NOT NULL,
    n_articles      INTEGER NOT NULL,
    sentiment_mean  DOUBLE PRECISION,
    sentiment_std   DOUBLE PRECISION,
    severity_max    INTEGER,
    classifier_version TEXT NOT NULL,
    refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_sentiment_daily_date ON sentiment_daily (date);

-- ----- Themes / trends -------------------------------------------------------

CREATE TABLE IF NOT EXISTS trend_snapshots (
    theme        TEXT NOT NULL,
    date         DATE NOT NULL,
    score        DOUBLE PRECISION NOT NULL,
    n_mentions   INTEGER,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (theme, date)
);

-- ----- Long-form documents (unifies email_research_* + manual_research_*) ----

CREATE TABLE IF NOT EXISTS documents (
    id              BIGSERIAL PRIMARY KEY,
    source_pipeline TEXT NOT NULL,        -- 'email_research' | 'manual_research' | 'idee_scraper'
    external_id     TEXT NOT NULL,        -- pipeline-side primary key
    title           TEXT NOT NULL,
    body            TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_pipeline, external_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_pipeline ON documents (source_pipeline, inserted_at DESC);

CREATE TABLE IF NOT EXISTS document_items (
    document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    item_type    TEXT NOT NULL,           -- 'paragraph', 'bullet', 'idea', …
    content      TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (document_id, ordinal)
);

-- ----- Briefings (Companies_News output) -------------------------------------

CREATE TABLE IF NOT EXISTS briefings (
    id          BIGSERIAL PRIMARY KEY,
    as_of       DATE NOT NULL,
    audience    TEXT NOT NULL,
    html_body   TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivered_at TIMESTAMPTZ,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (as_of, audience)
);

CREATE TABLE IF NOT EXISTS briefing_items (
    briefing_id BIGINT NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    ticker      TEXT,
    article_id  BIGINT REFERENCES articles(id) ON DELETE SET NULL,
    rationale   TEXT,
    PRIMARY KEY (briefing_id, ordinal)
);

-- ----- Pipeline observability ------------------------------------------------

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           BIGSERIAL PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL,
    rows_in      INTEGER,
    rows_out     INTEGER,
    error        TEXT,
    extras       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_name_started
    ON pipeline_runs (pipeline_name, started_at DESC);

COMMIT;
