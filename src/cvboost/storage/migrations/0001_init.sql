CREATE TABLE sources (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_override_s INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE source_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL REFERENCES sources(name),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
    found INTEGER,
    new INTEGER,
    invalid INTEGER,
    error_type TEXT,
    error_message TEXT,
    http_status INTEGER,
    duration_ms INTEGER
);
CREATE INDEX idx_source_runs_source_started ON source_runs (source, started_at DESC);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES jobs(id),
    source TEXT NOT NULL REFERENCES sources(name),
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    apply_url TEXT,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    description_html TEXT NOT NULL,
    description_text TEXT NOT NULL,
    location_text TEXT,
    remote INTEGER,
    employment_type TEXT,
    tags_json TEXT NOT NULL,
    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT,
    salary_period TEXT,
    salary_text TEXT,
    posted_at TEXT,
    fetched_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX idx_jobs_canonical ON jobs (canonical_id);
CREATE INDEX idx_jobs_content_hash ON jobs (content_hash);
CREATE INDEX idx_jobs_url ON jobs (url);
CREATE INDEX idx_jobs_posted_at ON jobs (posted_at);

CREATE TABLE sightings (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    run_id INTEGER NOT NULL REFERENCES source_runs(id),
    seen_at TEXT NOT NULL,
    UNIQUE (job_id, run_id)
);
