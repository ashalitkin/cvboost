-- #17: one row per e-mail (or per message unwrapped from a forward) read from the alerts mailbox.
-- Holds the UID cursor (MAX(uid) per mailbox+uidvalidity), dedupe by Message-ID hash and, only while a
-- message waits for the LLM fallback, the prepared text (llm_input, cleared afterwards).
-- Only the sender's domain is stored, never the address. Additive: old code ignores the table.
-- Rollback: DROP TABLE email_messages; DELETE FROM schema_migrations WHERE version = 14;
CREATE TABLE email_messages (
    id INTEGER PRIMARY KEY,
    mailbox TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    part INTEGER NOT NULL DEFAULT 0,              -- 0 = the message itself, n = n-th unwrapped inner message
    msgid_hash TEXT NOT NULL,                     -- sha256 of the (inner) Message-ID, or Date|From|Subject
    message_id TEXT CHECK (message_id IS NULL OR length(message_id) <= 300),
    sender_domain TEXT,
    platform TEXT,
    run_id INTEGER REFERENCES source_runs(id),
    received_at TEXT,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('parsed', 'needs_llm', 'llm_job', 'not_job', 'ignored', 'too_large', 'llm_error',
         'expired', 'dkim_fail', 'crm', 'crm_untrusted', 'duplicate', 'parse_error')),
    jobs_found INTEGER NOT NULL DEFAULT 0,
    llm_input TEXT CHECK (llm_input IS NULL OR length(llm_input) <= 8000),
    links_json TEXT CHECK (links_json IS NULL OR length(links_json) <= 8000),  -- canonical job links
    llm_attempts INTEGER NOT NULL DEFAULT 0,
    llm_call_id INTEGER,                          -- no FK: llm_calls is rebuilt by 0015
    note TEXT CHECK (note IS NULL OR length(note) <= 200),
    UNIQUE (mailbox, uidvalidity, uid, part)
);
CREATE INDEX idx_email_messages_status ON email_messages (status, id);
CREATE INDEX idx_email_messages_hash ON email_messages (msgid_hash);
