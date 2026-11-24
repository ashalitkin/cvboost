-- Additive only: rolling the code back without rolling the schema back is safe.
-- Rollback (manual; deletes the owner's application notes, so only on his say-so):
--   DROP TABLE application_events; DROP TABLE applications; DELETE FROM schema_migrations WHERE version = 5;
-- Owner of this schema is task #6 (web panel). The CV queue (`cv_requests`) belongs to #5/#7.

CREATE TABLE applications (
    canonical_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    status TEXT NOT NULL CHECK (status IN
        ('preparing', 'ready', 'applied', 'replied', 'interview', 'rejected', 'offer')),
    note TEXT CHECK (note IS NULL OR length(note) <= 2000),   -- owner text
    updated_at TEXT NOT NULL
);

CREATE TABLE application_events (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);
CREATE INDEX idx_app_events ON application_events (canonical_id, at);
