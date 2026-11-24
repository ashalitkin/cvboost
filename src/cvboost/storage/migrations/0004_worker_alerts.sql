-- Additive only: rolling the code back without rolling the schema back is safe.
-- Rollback (manual; deletes the owner's decisions, so only on his say-so):
--   DROP TABLE cv_requests; DROP TABLE decisions; DROP TABLE alerts; DROP TABLE system_alerts;
--   DROP TABLE worker_cycles; DROP TABLE worker_state; DELETE FROM schema_migrations WHERE version = 4;
-- Owner of this schema is task #5; #6 (panel) and #7 (CV drafting) read and write decisions and
-- cv_requests through the Store methods record_decision / request_cv / latest_decisions.

CREATE TABLE worker_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- keys: alerts_since, tg_offset, heartbeat, backfill:<local_day> (calls used that night)

CREATE TABLE worker_cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'error', 'interrupted')),
    sources_run INTEGER,
    sources_deferred INTEGER,
    new_jobs INTEGER,
    scored INTEGER,
    score_stopped TEXT,
    alerts_sent INTEGER,
    alerts_summarized INTEGER,
    error TEXT,
    duration_ms INTEGER
);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),          -- representative at alert time
    llm_verdict_id INTEGER REFERENCES verdicts(id),
    fit INTEGER,
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'sent', 'summarized', 'skipped_closed', 'skipped_backlog', 'skipped_stale',
         'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    chat_id INTEGER,
    message_id INTEGER,
    text TEXT,                                            -- exactly as sent
    last_error TEXT,                                      -- redacted, <= 300 chars
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX idx_alerts_status ON alerts (status, created_at);
CREATE INDEX idx_alerts_job ON alerts (job_id);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES jobs(id),    -- group at decision time
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    action TEXT NOT NULL CHECK (action IN ('interested', 'rejected', 'cv_requested', 'undo')),
    reason TEXT CHECK (reason IS NULL OR reason IN ('stack', 'rate', 'location', 'seniority', 'other')),
    note TEXT CHECK (note IS NULL OR length(note) <= 200),  -- free text, panel only
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'panel')),
    alert_id INTEGER REFERENCES alerts(id),
    tg_update_id INTEGER UNIQUE,                          -- idempotent on redelivery
    at TEXT NOT NULL
);
CREATE INDEX idx_decisions_job ON decisions (job_id, at DESC);
CREATE INDEX idx_decisions_canonical ON decisions (canonical_id, at DESC);

CREATE TABLE cv_requests (                                -- queue consumed by #7
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES jobs(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'done', 'failed', 'cancelled')),
    kit_dir TEXT,                                         -- #7 fills; relative to CVBOOST_KITS_DIR
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT CHECK (error IS NULL OR length(error) <= 300)  -- untrusted text
);
CREATE UNIQUE INDEX uq_cv_requests_active ON cv_requests (job_id)
    WHERE status IN ('queued', 'in_progress');

CREATE TABLE system_alerts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (kind, dedupe_key)
);
