-- Phase 9b: webform/URL outreach route (mirror of SQLite schema v5).
--
-- outreach_class: 'email' (historical default) or 'webform' (targeted via
-- submission_url).  Constant-default ADD COLUMN is metadata-only in
-- PG 11+, so this stays well under the 8s statement_timeout even on
-- large tables.  IF NOT EXISTS keeps the statement idempotent.
--
-- NOTE: email stays NOT NULL (matching the SQLite v5 schema); webform
-- targets are carried by the nullable submission_url column.
ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS outreach_class TEXT NOT NULL DEFAULT 'email';

ALTER TABLE outreach_messages
    ADD COLUMN IF NOT EXISTS submission_url TEXT;
