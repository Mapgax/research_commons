-- =============================================================================
-- DB1: market — risk_scores, risk_monitor's model output
-- Target: Aiven Postgres 16+
--
-- risk_monitor (~/Projects/AI/risk_monitor, separate sibling repo) computes
-- underperformance/correction probabilities per ticker and, until now, wrote
-- them ONLY to a local artifacts/scores.parquet file — never to a database,
-- so "clean, updated data in market_db" never actually included this output.
-- This table is the writer target added in scoring.py (fit_and_score) to fix
-- that; the parquet file stays as a local/offline cache, this table is now
-- the source of truth other services (build_risk_artifact.py, MSARN) read.
--
-- Column set matches scoring.py's `keep` list exactly (2026-07-30). ticker
-- FKs to the same ticker_metadata this migration set's earlier steps
-- consolidated, with ON UPDATE CASCADE like every other per-ticker table
-- (prices, fundamentals, features, alt_data, insider_transactions,
-- option_metrics) — a rename-ticker run cascades here too.
--
-- run_scope records whether a given (ticker, date) row came from an
-- active-only or covered (active+inactive) refresh — see scoring.py's
-- scope="active"|"covered" split, which exists so inactive names can run on
-- a slower cadence without an active-scope refresh overwriting their more
-- complete covered-scope row with a partial one on the same date.
--
-- Idempotent: every step is guarded or a no-op on re-run.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS risk_scores (
    ticker              TEXT NOT NULL REFERENCES ticker_metadata(ticker) ON UPDATE CASCADE,
    date                DATE NOT NULL,
    close               DOUBLE PRECISION,
    sector_key          TEXT,
    p_underperf         DOUBLE PRECISION,
    p_correction        DOUBLE PRECISION,
    p_correction_ml     DOUBLE PRECISION,
    p_correction_vol    DOUBLE PRECISION,
    p_correction_garch  DOUBLE PRECISION,
    p_under_regime      DOUBLE PRECISION,
    p_stay_h            DOUBLE PRECISION,
    rs_level            DOUBLE PRECISION,
    rs_mom_63           DOUBLE PRECISION,
    rs_tstat_63         DOUBLE PRECISION,
    rs_dd_252           DOUBLE PRECISION,
    rs_below_ma_dur     DOUBLE PRECISION,
    vol_21              DOUBLE PRECISION,
    ewma_vol            DOUBLE PRECISION,
    dd_252              DOUBLE PRECISION,
    runup_252           DOUBLE PRECISION,
    vix                 DOUBLE PRECISION,
    sent_mean_7d        DOUBLE PRECISION,
    article_surge       DOUBLE PRECISION,
    severity_max_7d     DOUBLE PRECISION,
    n_articles_7d       DOUBLE PRECISION,
    -- Walk-forward labels — populated for historical rows used in model
    -- evaluation, NULL for the live/latest row (the outcome hasn't happened
    -- yet). Kept here rather than split into a separate table because
    -- scoring.py already carries them through the same per-(ticker,date) frame.
    y_rel_under         DOUBLE PRECISION,
    y_correction        DOUBLE PRECISION,
    fwd_trough_ret       DOUBLE PRECISION,
    fwd_rel_ret          DOUBLE PRECISION,
    run_scope           TEXT NOT NULL DEFAULT 'covered' CHECK (run_scope IN ('active', 'covered')),
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_risk_scores_date ON risk_scores (date);

COMMIT;
