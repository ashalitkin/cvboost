-- cvboost: foreign_keys=off
-- #27: decisions.action gains 'override_in' (owner returns an AI-screened job to the Inbox with an
-- explanation) and decisions.note grows from 200 to 500 characters.
-- SQLite cannot change a CHECK, so the table is rebuilt (12-step procedure) with the same columns,
-- keys and indexes; every row is copied as it is. cv_requests (and alerts) refer to decisions(id)
-- by name; ids are preserved, so the references stay valid.
-- Rollback: only while no 'override_in' row and no note longer than 200 chars exists (otherwise
-- delete/shorten them first, with the owner's say-so): rebuild the table with the 0004 CHECKs;
--   DELETE FROM schema_migrations WHERE version = 10;
CREATE TABLE decisions_new (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES jobs(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    action TEXT NOT NULL CHECK (
        action IN ('interested', 'rejected', 'cv_requested', 'undo', 'override_in')
    ),
    reason TEXT CHECK (reason IS NULL OR reason IN ('stack', 'rate', 'location', 'seniority', 'other')),
    note TEXT CHECK (note IS NULL OR length(note) <= 500),
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'panel')),
    alert_id INTEGER REFERENCES alerts(id),
    tg_update_id INTEGER UNIQUE,
    at TEXT NOT NULL
);
INSERT INTO decisions_new (id, canonical_id, job_id, action, reason, note, channel, alert_id,
                           tg_update_id, at)
SELECT id, canonical_id, job_id, action, reason, note, channel, alert_id, tg_update_id, at
FROM decisions;
DROP TABLE decisions;
ALTER TABLE decisions_new RENAME TO decisions;
CREATE INDEX idx_decisions_job ON decisions (job_id, at DESC);
CREATE INDEX idx_decisions_canonical ON decisions (canonical_id, at DESC);
