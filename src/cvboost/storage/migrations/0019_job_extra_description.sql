-- 0019: owner's extra job description (#41): text pasted from the original posting when the board
-- gave too little, one per duplicate group, kept apart from jobs.description_text; plus a queue
-- for one-shot Haiku re-scores of a group. Additive tables; existing rows unchanged.
-- Rollback: DROP TABLE rescore_requests; DROP TABLE job_extra_descriptions;
--           DELETE FROM schema_migrations WHERE version = 19;

CREATE TABLE job_extra_descriptions (
    canonical_id INTEGER PRIMARY KEY,
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 20000),
    updated_at TEXT NOT NULL,
    use_in_score INTEGER NOT NULL DEFAULT 0 CHECK (use_in_score IN (0, 1))
);

CREATE TABLE rescore_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'done', 'failed')),
    requested_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE UNIQUE INDEX rescore_one_queued ON rescore_requests (canonical_id) WHERE status = 'queued';
