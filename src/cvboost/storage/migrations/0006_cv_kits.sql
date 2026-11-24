-- cvboost: foreign_keys=off
-- Rebuilds llm_calls (kind 'cv') and cv_requests (status 'needs_review', attempt/review columns).
-- migrate() runs this with foreign_keys=OFF, then foreign_key_check must be empty before COMMIT.
-- Old code on the new schema keeps working: it never writes kind='cv' and only reads statuses.
-- Independent of the 0005 tables (applications): it touches only llm_calls and cv_requests.
-- Rollback (manual, only when needed; `.backup` first; run with PRAGMA foreign_keys=OFF):
--   1. UPDATE cv_requests SET status = 'done' WHERE status = 'needs_review';
--      UPDATE cv_requests SET last_call_id = NULL;
--   2. DELETE FROM llm_calls WHERE kind = 'cv';   (no llm_cache / llm_item_errors rows point at
--      cv calls: only scoring writes them)
--   3. Rebuild llm_calls with the 0003 CHECK (kind IN ('score','preflight')) and cv_requests
--      with the 0004 columns and CHECK: new table -> copy the shared columns -> drop -> rename,
--      then recreate idx_llm_calls_day, idx_llm_calls_pause and uq_cv_requests_active.
--   4. PRAGMA foreign_key_check must print nothing; DELETE FROM schema_migrations WHERE version = 6.

CREATE TABLE llm_calls_new (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('score', 'preflight', 'cv')),
    model_alias TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    n_items INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    local_day TEXT NOT NULL,
    duration_ms INTEGER,
    status TEXT NOT NULL CHECK (status IN
      ('started', 'ok', 'partial', 'invalid_output', 'error', 'timeout', 'usage_limit', 'auth_error',
       'budget', 'refused_auth_method')),
    exit_code INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    total_cost_usd REAL,
    error_message TEXT,
    auth_method TEXT,
    pause_until TEXT,
    credential_expires_at TEXT
);
INSERT INTO llm_calls_new (id, kind, model_alias, model, prompt_version, n_items, attempt,
    started_at, local_day, duration_ms, status, exit_code, input_tokens, output_tokens,
    cache_read_tokens, cache_creation_tokens, total_cost_usd, error_message, auth_method,
    pause_until, credential_expires_at)
SELECT id, kind, model_alias, model, prompt_version, n_items, attempt,
    started_at, local_day, duration_ms, status, exit_code, input_tokens, output_tokens,
    cache_read_tokens, cache_creation_tokens, total_cost_usd, error_message, auth_method,
    pause_until, credential_expires_at
FROM llm_calls;
DROP TABLE llm_calls;
ALTER TABLE llm_calls_new RENAME TO llm_calls;
CREATE INDEX idx_llm_calls_day ON llm_calls (local_day, model_alias);
CREATE INDEX idx_llm_calls_pause ON llm_calls (pause_until);

CREATE TABLE cv_requests_new (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES jobs(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    status TEXT NOT NULL CHECK (status IN
        ('queued', 'in_progress', 'done', 'failed', 'cancelled', 'needs_review')),
    kit_dir TEXT,                                         -- relative to CVBOOST_KITS_DIR
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT CHECK (error IS NULL OR length(error) <= 300),  -- untrusted text
    attempts INTEGER NOT NULL DEFAULT 0,
    last_call_id INTEGER REFERENCES llm_calls(id),
    prompt_version TEXT,
    profile_sha TEXT,
    review_reasons TEXT CHECK (review_reasons IS NULL OR length(review_reasons) <= 2000),  -- JSON
    feedback TEXT CHECK (feedback IS NULL OR length(feedback) <= 2000),
    edited_at TEXT,
    reviewed_at TEXT,
    pdf_status TEXT CHECK (pdf_status IS NULL OR pdf_status IN ('ok', 'failed', 'stale', 'unavailable'))
);
INSERT INTO cv_requests_new (id, canonical_id, job_id, decision_id, status, kit_dir, requested_at,
    started_at, finished_at, error)
SELECT id, canonical_id, job_id, decision_id, status, kit_dir, requested_at,
    started_at, finished_at, error
FROM cv_requests;
DROP TABLE cv_requests;
ALTER TABLE cv_requests_new RENAME TO cv_requests;
CREATE UNIQUE INDEX uq_cv_requests_active ON cv_requests (job_id)
    WHERE status IN ('queued', 'in_progress');
