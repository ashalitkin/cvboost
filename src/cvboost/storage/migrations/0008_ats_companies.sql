-- ATS company state (#18). Additive only: one new table, nothing existing is touched, so old code
-- keeps working on the new schema. The company LIST lives in config/companies.yaml; this table
-- holds what the worker learned about each entry: when it was last polled, whether the ATS
-- still knows it (404/410 twice, 6 h apart -> active = 0; an inactive company is re-checked
-- every 30 days). next_due_at is when it is polled next (NULL = never polled yet).
-- Rollback: DROP TABLE ats_companies; DELETE FROM schema_migrations WHERE version = 8;
CREATE TABLE ats_companies (
    ats TEXT NOT NULL CHECK (ats IN ('greenhouse', 'lever', 'ashby')),
    slug TEXT NOT NULL CHECK (length(slug) BETWEEN 1 AND 64),
    name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    added_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_ok_at TEXT,
    last_status INTEGER,
    last_found INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    gone_strikes INTEGER NOT NULL DEFAULT 0,
    next_due_at TEXT,
    inactive_since TEXT,
    inactive_reason TEXT,
    PRIMARY KEY (ats, slug)
);
CREATE INDEX idx_ats_companies_due ON ats_companies (ats, next_due_at);
