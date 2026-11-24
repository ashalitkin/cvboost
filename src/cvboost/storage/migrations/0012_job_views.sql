-- #30: «просмотрено / новое» like a mailbox. One row per duplicate group (canonical job id) the owner
-- has seen: he opened its page or made any decision on it (panel or Telegram). Absent row = unseen.
-- New table only; nothing existing changes. Jobs the owner already decided on or applied to are
-- backfilled as seen (at the time of the first decision), everything else starts unseen.
-- Rollback: DROP TABLE job_views;  DELETE FROM schema_migrations WHERE version = 12;
CREATE TABLE job_views (
    canonical_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    seen_at TEXT NOT NULL
);
INSERT INTO job_views (canonical_id, seen_at)
SELECT canonical_id, MIN(at) FROM decisions GROUP BY canonical_id;
INSERT OR IGNORE INTO job_views (canonical_id, seen_at)
SELECT canonical_id, updated_at FROM applications;
