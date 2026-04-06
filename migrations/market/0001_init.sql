-- =============================================================================
-- DB1: market — initial schema
-- Target: Aiven Postgres 16+
-- See ARCHITECTURE_REFACTOR.md §4.1 for the design rationale.
--
-- Idempotent: every CREATE uses IF NOT EXISTS.
-- Run as the database owner role; afterwards apply role grants from
-- ARCHITECTURE_REFACTOR.md §6 ("msarn_writer", "dashboard_reader").
-- =============================================================================

BEGIN;

-- ----- Reference / metadata --------------------------------------------------

CREATE TABLE IF NOT EXISTS ticker_metadata (
    ticker            TEXT PRIMARY KEY,
    company_name      TEXT NOT NULL,
    isin              TEXT,
    bloomberg_ticker  TEXT,
    yahoo_symbol      TEXT NOT NULL,
    sector            TEXT,
    currency          CHAR(3) NOT NULL,         -- ISO 4217
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    added_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS sector_etf_registry (
    sector            TEXT PRIMARY KEY,
    etf_ticker        TEXT NOT NULL,
    notes             TEXT
);

-- ----- Time series: prices, fundamentals -------------------------------------

CREATE TABLE IF NOT EXISTS prices (
    ticker     TEXT        NOT NULL REFERENCES ticker_metadata(ticker) ON UPDATE CASCADE,
    date       DATE        NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION NOT NULL,
    adj_close  DOUBLE PRECISION,
    volume     DOUBLE PRECISION,
    currency   CHAR(3)     NOT NULL DEFAULT 'USD',
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices (date);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker            TEXT NOT NULL REFERENCES ticker_metadata(ticker) ON UPDATE CASCADE,
    as_of             DATE NOT NULL,
    trailing_pe       DOUBLE PRECISION,
    forward_pe        DOUBLE PRECISION,
    price_to_book     DOUBLE PRECISION,
    debt_to_equity    DOUBLE PRECISION,
    return_on_equity  DOUBLE PRECISION,
    profit_margins    DOUBLE PRECISION,
    ev_to_ebitda      DOUBLE PRECISION,
    inserted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, as_of)
);

-- ----- Macro / cross-asset / alt-data ----------------------------------------

CREATE TABLE IF NOT EXISTS macro_data (
    date               DATE PRIMARY KEY,
    vix                DOUBLE PRECISION,
    vix_5d_change      DOUBLE PRECISION,
    us10y_yield        DOUBLE PRECISION,
    us2y_yield         DOUBLE PRECISION,
    yield_curve_10y2y  DOUBLE PRECISION,
    hy_spread          DOUBLE PRECISION,
    dollar_index       DOUBLE PRECISION,
    put_call_ratio     DOUBLE PRECISION,
    inserted_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cross_asset (
    asset       TEXT NOT NULL,         -- e.g. SPY, TLT, GLD, ^VIX, ACWI, sector ETFs
    date        DATE NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    return_1d   DOUBLE PRECISION,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset, date)
);

CREATE TABLE IF NOT EXISTS insider_transactions (
    ticker        TEXT NOT NULL REFERENCES ticker_metadata(ticker) ON UPDATE CASCADE,
    filing_url    TEXT NOT NULL,
    insider_name  TEXT,
    transaction_date DATE,
    transaction_type TEXT,
    shares        DOUBLE PRECISION,
    price         DOUBLE PRECISION,
    inserted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, filing_url)
);

CREATE TABLE IF NOT EXISTS option_metrics (
    ticker         TEXT NOT NULL REFERENCES ticker_metadata(ticker) ON UPDATE CASCADE,
    date           DATE NOT NULL,
    atm_iv_30d     DOUBLE PRECISION,
    atm_iv_60d     DOUBLE PRECISION,
    put_skew_30d   DOUBLE PRECISION,
    iv_rank_52w    DOUBLE PRECISION,
    put_call_oi_ratio DOUBLE PRECISION,
    call_volume    DOUBLE PRECISION,
    put_volume     DOUBLE PRECISION,
    inserted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS alt_data (
    ticker       TEXT NOT NULL REFERENCES ticker_metadata(ticker) ON UPDATE CASCADE,
    date         DATE NOT NULL,
    source       TEXT NOT NULL,         -- e.g. google_trends, edgar_form4
    metric       TEXT NOT NULL,
    value        DOUBLE PRECISION,
    inserted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date, source, metric)
);

-- ----- Engineered features ---------------------------------------------------
-- Wide table; column list mirrors models/trainer.py FEATURE_COLS. Use ALTER TABLE
-- ADD COLUMN for additions; do not silently rename columns.

CREATE TABLE IF NOT EXISTS features (
    ticker      TEXT NOT NULL REFERENCES ticker_metadata(ticker) ON UPDATE CASCADE,
    date        DATE NOT NULL,
    -- Feature columns are added by ALTER TABLE in subsequent migrations.
    -- The minimum required columns are:
    close              DOUBLE PRECISION,
    returns_1d         DOUBLE PRECISION,
    -- (~70 more columns; see ARCHITECTURE_REFACTOR.md §4.1.10 for the full list)
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_features_date ON features (date);

-- ----- Model registry / backtests --------------------------------------------

CREATE TABLE IF NOT EXISTS model_registry (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    target          TEXT NOT NULL,
    architecture    TEXT NOT NULL,        -- 'cnn_lstm', 'msarn', 'logreg', …
    checkpoint_path TEXT,
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    trained_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_model_registry_ticker_target
    ON model_registry (ticker, target, trained_at DESC);

CREATE TABLE IF NOT EXISTS model_benchmarks (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    target      TEXT NOT NULL,
    metrics     JSONB NOT NULL,
    trained_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS regime_models (
    id           BIGSERIAL PRIMARY KEY,
    family       TEXT NOT NULL,        -- 'hmm', 'markov_switching'
    path         TEXT NOT NULL,
    config       JSONB NOT NULL DEFAULT '{}'::jsonb,
    trained_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rl_portfolio (
    id           BIGSERIAL PRIMARY KEY,
    policy_name  TEXT NOT NULL,
    state        JSONB NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backtest_results (
    model_id     BIGINT NOT NULL REFERENCES model_registry(id) ON DELETE CASCADE,
    date         DATE NOT NULL,
    p_event      DOUBLE PRECISION,
    realized     DOUBLE PRECISION,
    decision     INTEGER,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (model_id, date)
);

-- ----- Scheduling / app state ------------------------------------------------

CREATE TABLE IF NOT EXISTS scheduler_leases (
    name        TEXT PRIMARY KEY,
    holder      TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL,
    error        TEXT,
    extras       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_name_started
    ON scheduler_runs (name, started_at DESC);

CREATE TABLE IF NOT EXISTS app_state (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
