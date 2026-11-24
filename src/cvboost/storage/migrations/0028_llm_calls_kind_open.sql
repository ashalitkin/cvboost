-- cvboost: foreign_keys=off
-- #57: llm_calls.kind stops being a closed list. The CHECK only guards the format
-- ([a-z_], 1..32 chars); the allowed kinds are a registry in code: the core's
-- {score, preflight, cv, email} plus what each loaded plugin declares (Store.insert_llm_call).
-- llm_calls is rebuilt like in 0015/0023; rows are copied unchanged (including the kinds of plugins).
-- Rollback: rebuild llm_calls with the CHECK of 0015 (kind IN ('score','preflight','cv','email'))
-- or of 0023 (plus the kinds of the plugin that owned 0023) after deleting rows whose kind is not in
-- that list, recreate idx_llm_calls_day and idx_llm_calls_pause; PRAGMA foreign_key_check must
-- print nothing; DELETE FROM schema_migrations WHERE version = 28.
-- Note: a database migrated by the core only (no plugins) that later gets a plugin
-- applies that plugin's 0023 afterwards, which narrows the CHECK back to its list; the Store registry
-- still decides, so only a kind outside 0023's list would be refused by that older CHECK.

CREATE TABLE llm_calls_new (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 32 AND kind NOT GLOB '*[^a-z_]*'),
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
