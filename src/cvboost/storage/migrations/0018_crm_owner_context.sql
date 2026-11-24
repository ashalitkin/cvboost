-- 0018: CRM owner context (#39): the owner's own text per contact (his first message, notes) and
-- the optional e-mail block of a reply draft. Additive, nullable columns; existing rows unchanged.
-- Rollback: ALTER TABLE crm_contacts DROP COLUMN owner_context;
--           ALTER TABLE crm_reply_requests DROP COLUMN email_to;
--           ALTER TABLE crm_reply_requests DROP COLUMN email_subject;
--           ALTER TABLE crm_reply_requests DROP COLUMN email_body;
--           DELETE FROM schema_migrations WHERE version = 18;

ALTER TABLE crm_contacts ADD COLUMN owner_context TEXT
    CHECK (owner_context IS NULL OR length(owner_context) <= 4000);
ALTER TABLE crm_reply_requests ADD COLUMN email_to TEXT
    CHECK (email_to IS NULL OR length(email_to) <= 254);
ALTER TABLE crm_reply_requests ADD COLUMN email_subject TEXT
    CHECK (email_subject IS NULL OR length(email_subject) <= 200);
ALTER TABLE crm_reply_requests ADD COLUMN email_body TEXT
    CHECK (email_body IS NULL OR length(email_body) <= 4000);
