-- cvboost: foreign_keys=off
-- #29 «Передумал»: applications.status gains 'withdrawn' (SQLite cannot change a CHECK, so the table
-- is rebuilt with the same columns; every row is copied as it is; nothing refers to applications),
-- and decisions gets `withdrawn` (1 = the 'rejected' row written by «Передумал» on an application,
-- so the OWNER CORRECTIONS block can say «changed his mind after choosing»). ADD COLUMN, no rebuild.
-- Rollback: only while no application is 'withdrawn' (otherwise move them to another status with
-- the owner's say-so): rebuild applications with the 0005 CHECK;
--   ALTER TABLE decisions DROP COLUMN withdrawn;  DELETE FROM schema_migrations WHERE version = 11;
CREATE TABLE applications_new (
    canonical_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    status TEXT NOT NULL CHECK (status IN
        ('preparing', 'ready', 'applied', 'replied', 'interview', 'rejected', 'offer',
         'withdrawn')),
    note TEXT CHECK (note IS NULL OR length(note) <= 2000),
    updated_at TEXT NOT NULL
);
INSERT INTO applications_new (canonical_id, status, note, updated_at)
SELECT canonical_id, status, note, updated_at FROM applications;
DROP TABLE applications;
ALTER TABLE applications_new RENAME TO applications;

ALTER TABLE decisions ADD COLUMN withdrawn INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0, 1));
