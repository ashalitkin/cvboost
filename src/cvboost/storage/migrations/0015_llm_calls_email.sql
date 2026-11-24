-- cvboost: foreign_keys=off
-- #17b: llm_calls.kind gains 'email' (Haiku fallback parser for unknown alert / recruiter mails).
-- Rebuild per the 0006 pattern; the CHECK is widened, so old code keeps working.
-- Rollback: DELETE FROM llm_calls WHERE kind = 'email'; rebuild llm_calls with the 0006 CHECK
-- (kind IN ('score','preflight','cv')), recreate idx_llm_calls_day / idx_llm_calls_pause;
-- PRAGMA foreign_key_check must print nothing; DELETE FROM schema_migrations WHERE version = 15.

CREATE TABLE llm_calls_new (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('score', 'preflight', 'cv', 'email')),
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

