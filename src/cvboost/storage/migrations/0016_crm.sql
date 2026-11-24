-- #33 mini-CRM: LinkedIn contacts, their events, and Opus reply-draft requests. Additive only.
-- Reminders reuse system_alerts (kind 'crm_reminder'); drafts reuse the shared Opus cap by
-- logging their calls to llm_calls as kind 'cv' (prompt_version tells them apart), so llm_calls
-- and cv_requests are NOT touched.
-- Rollback: DROP TABLE crm_reply_requests; DROP TABLE crm_events; DROP TABLE crm_contacts;
--           DELETE FROM schema_migrations WHERE version = 16;

CREATE TABLE crm_contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(name) <= 200),
    name_key TEXT NOT NULL,                 -- casefolded name; identity only when there is no slug
    profile_slug TEXT CHECK (profile_slug IS NULL OR length(profile_slug) <= 200),
    headline TEXT CHECK (headline IS NULL OR length(headline) <= 300),
    company TEXT CHECK (company IS NULL OR length(company) <= 200),
    profile_url TEXT CHECK (profile_url IS NULL OR (profile_url LIKE 'https://%linkedin.com/%'
                                                    AND length(profile_url) <= 500)),
    kind TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ('recruiter', 'client', 'colleague', 'other')),
    col TEXT NOT NULL DEFAULT 'inbox' CHECK (col IN ('inbox', 'leads', 'talk')),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    note TEXT CHECK (note IS NULL OR length(note) <= 2000),
    next_step TEXT CHECK (next_step IS NULL OR length(next_step) <= 300),
    due_at TEXT CHECK (due_at IS NULL OR due_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    last_event_at TEXT,                      -- newest event's own received_at (UTC iso)
    unread INTEGER NOT NULL DEFAULT 1 CHECK (unread IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- identity: the LinkedIn /in/<slug> when known (unique), else the name
CREATE UNIQUE INDEX uq_crm_contacts_slug ON crm_contacts (profile_slug) WHERE profile_slug IS NOT NULL;
CREATE INDEX idx_crm_contacts_name ON crm_contacts (name_key);
CREATE INDEX idx_crm_contacts_order ON crm_contacts (archived, col, last_event_at);

CREATE TABLE crm_events (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('invitation_accepted', 'message', 'message_accepted', 'other')),
    received_at TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '' CHECK (length(snippet) <= 500),
    view_url TEXT CHECK (view_url IS NULL OR (view_url LIKE 'https://%linkedin.com/%'
                                              AND length(view_url) <= 500)),
    mail_message_id TEXT,
    dedupe_key TEXT NOT NULL,               -- message_id#index, or hash of kind|time|snippet
    created_at TEXT NOT NULL,
    UNIQUE (contact_id, dedupe_key)
);
CREATE INDEX idx_crm_events_contact ON crm_events (contact_id, received_at);

CREATE TABLE crm_reply_requests (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'done', 'failed', 'cancelled')),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    feedback TEXT CHECK (feedback IS NULL OR length(feedback) <= 2000),
    draft TEXT CHECK (draft IS NULL OR length(draft) <= 4000),
    error TEXT CHECK (error IS NULL OR length(error) <= 300),
    last_call_id INTEGER REFERENCES llm_calls(id),
    prompt_version TEXT,
    profile_sha TEXT
);
CREATE UNIQUE INDEX uq_crm_reply_active ON crm_reply_requests (contact_id)
    WHERE status IN ('queued', 'in_progress');
