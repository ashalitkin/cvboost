-- Opus verdict on the CV request, shown in the panel («Почему»). ADD COLUMN only, no rebuild:
-- old code on the new schema never writes them and ignores them; NULL = no verdict stored.
-- Existing kits are filled by `cvboost cv backfill-notes` (from fit_notes.md).
-- Rollback: nothing needed (the columns are ignored); DELETE FROM schema_migrations WHERE version = 7
-- and ALTER TABLE cv_requests DROP COLUMN ... if the columns must go.
ALTER TABLE cv_requests ADD COLUMN recommendation TEXT
    CHECK (recommendation IS NULL OR recommendation IN ('apply', 'apply_with_caveats', 'skip'));
ALTER TABLE cv_requests ADD COLUMN fit_summary TEXT
    CHECK (fit_summary IS NULL OR length(fit_summary) <= 600);
