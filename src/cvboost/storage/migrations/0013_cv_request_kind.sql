-- #32: a cover-letter-only request for a kit that already exists. Same queue, same Opus cap.
-- 'cv' = the full kit (all existing rows), 'cover' = only cover_letter.md/pdf; a 'cover' request
-- never touches the CV verdict, review state or resume files and is not the group's "latest CV".
-- Rollback: UPDATE cv_requests SET status='cancelled' WHERE kind='cover' AND status IN ('queued','in_progress');
--   then DROP INDEX uq_cv_requests_active; CREATE UNIQUE INDEX uq_cv_requests_active ON cv_requests (job_id)
--   WHERE status IN ('queued','in_progress'); ALTER TABLE cv_requests DROP COLUMN kind;  DELETE FROM schema_migrations WHERE version = 13;
ALTER TABLE cv_requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'cv' CHECK (kind IN ('cv', 'cover'));
-- one active request per job AND kind: a queued letter must not swallow «Готовить CV»
DROP INDEX uq_cv_requests_active;
CREATE UNIQUE INDEX uq_cv_requests_active ON cv_requests (job_id, kind)
    WHERE status IN ('queued', 'in_progress');
