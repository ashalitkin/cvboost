-- Rollback (manual, SQLite >= 3.35): DROP INDEX idx_verdicts_job; DROP INDEX idx_verdicts_version;
-- DROP TABLE verdicts; DROP INDEX idx_jobs_dedupe;
-- ALTER TABLE jobs DROP COLUMN title_norm; ALTER TABLE jobs DROP COLUMN company_norm;
-- DELETE FROM schema_migrations WHERE version = 2;
ALTER TABLE jobs ADD COLUMN company_norm TEXT;
ALTER TABLE jobs ADD COLUMN title_norm TEXT;
CREATE INDEX idx_jobs_dedupe ON jobs (company_norm, title_norm);

CREATE TABLE verdicts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    stage TEXT NOT NULL,                 -- 'rules' now; #4 adds 'llm' without a migration
    decision TEXT NOT NULL CHECK (decision IN ('keep', 'reject')),
    reasons_json TEXT NOT NULL,          -- ["us_only", "rate_below_min", ...] all triggered reject reasons
    flags_json TEXT NOT NULL,            -- ["rate_unknown"]
    relevance INTEGER,                   -- 0..100, max over profiles
    profile TEXT,                        -- data_engineer | marketing_analytics
    details_json TEXT NOT NULL,          -- evidence per reason, rate parse, keyword hits per profile
    rule_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    at TEXT NOT NULL,
    UNIQUE (job_id, stage, rule_version, input_hash)
);
CREATE INDEX idx_verdicts_job ON verdicts (job_id, stage, rule_version, at DESC);
CREATE INDEX idx_verdicts_version ON verdicts (stage, rule_version);
