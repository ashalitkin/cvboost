-- Additive only. Call budget of the keyed APIs (#19): one row per API per UTC day; the month is
-- the sum of the days of that month. Counts only; no keys, no URLs.
-- Rollback (manual): DROP TABLE api_calls; DELETE FROM schema_migrations WHERE version = 17;
CREATE TABLE api_calls (
    api TEXT NOT NULL,
    day TEXT NOT NULL,                       -- YYYY-MM-DD (UTC)
    calls INTEGER NOT NULL DEFAULT 0 CHECK (calls >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (api, day)
);
