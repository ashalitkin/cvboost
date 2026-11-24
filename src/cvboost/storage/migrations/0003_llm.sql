-- Additive only: rolling the code back without rolling the schema back is safe.
-- Rollback (manual): DROP TABLE llm_item_errors; DROP TABLE llm_cache; DROP TABLE llm_calls;
-- DELETE FROM schema_migrations WHERE version = 3;
-- LLM verdicts (verdicts.stage = 'llm'): DELETE FROM verdicts WHERE stage = 'llm'; (owner's call only)
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('score', 'preflight')),
    model_alias TEXT NOT NULL,            -- 'haiku' (from config)
    model TEXT,                           -- reported model, first key of .modelUsage
    prompt_version TEXT,
    n_items INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,   -- 2 = retry
    started_at TEXT NOT NULL,             -- row is written as status 'started' before the call
    local_day TEXT NOT NULL,              -- YYYY-MM-DD in the config timezone
    duration_ms INTEGER,
    status TEXT NOT NULL CHECK (status IN
      ('started', 'ok', 'partial', 'invalid_output', 'error', 'timeout', 'usage_limit', 'auth_error',
       'budget', 'refused_auth_method')),
    exit_code INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    total_cost_usd REAL,                  -- as reported (notional on a subscription)
    error_message TEXT,                   -- <= 300 chars of .result / stderr; never stdin or env
    auth_method TEXT,
    pause_until TEXT,                     -- set for usage_limit / auth_error / refused_auth_method
    credential_expires_at TEXT
);
CREATE INDEX idx_llm_calls_day ON llm_calls (local_day, model_alias);
CREATE INDEX idx_llm_calls_pause ON llm_calls (pause_until);

CREATE TABLE llm_cache (
    id INTEGER PRIMARY KEY,
    item_hash TEXT NOT NULL,
    model_alias TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    output_json TEXT NOT NULL,            -- validated JobVerdict (without ref)
    call_id INTEGER REFERENCES llm_calls(id),
    created_at TEXT NOT NULL,
    UNIQUE (item_hash, model_alias, prompt_version)
);

CREATE TABLE llm_item_errors (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),   -- representative
    item_hash TEXT NOT NULL,
    model_alias TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    call_id INTEGER REFERENCES llm_calls(id),
    error TEXT NOT NULL,                  -- invalid_output | missing_ref | error | timeout | budget
    at TEXT NOT NULL,
    UNIQUE (item_hash, model_alias, prompt_version)
);
