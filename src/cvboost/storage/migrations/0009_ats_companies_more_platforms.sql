-- cvboost: foreign_keys=off
-- #24: the ATS whitelist of ats_companies gains workable, recruitee, teamtailor, personio, gem.
-- SQLite cannot change a CHECK, so the table is rebuilt (12-step procedure) with the same
-- columns, keys and index; every row is copied as it is. No other table refers to it.
-- Rollback: only while no row of the new platforms exists (otherwise delete them first):
--   rebuild the table with the 0008 CHECK (greenhouse, lever, ashby);
--   DELETE FROM schema_migrations WHERE version = 9;
CREATE TABLE ats_companies_new (
    ats TEXT NOT NULL CHECK (
        ats IN ('greenhouse', 'lever', 'ashby', 'workable', 'recruitee', 'teamtailor',
                'personio', 'gem')
    ),
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
INSERT INTO ats_companies_new (
    ats, slug, name, source, active, added_at, last_checked_at, last_ok_at, last_status,
    last_found, consecutive_failures, gone_strikes, next_due_at, inactive_since, inactive_reason
) SELECT
    ats, slug, name, source, active, added_at, last_checked_at, last_ok_at, last_status,
    last_found, consecutive_failures, gone_strikes, next_due_at, inactive_since, inactive_reason
FROM ats_companies;
DROP TABLE ats_companies;
ALTER TABLE ats_companies_new RENAME TO ats_companies;
CREATE INDEX idx_ats_companies_due ON ats_companies (ats, next_due_at);
