-- First-class ownership for outreach records (mirror of SQLite schema v15).
-- ``owner_user_id`` records which authenticated operator created the outreach
-- record; it is NULL for legacy or anonymous records so the historical shared
-- single-operator behavior is preserved and those rows remain visible.
-- Additive: no existing column or row is touched, and the value is never
-- published in the public outreach projection.

ALTER TABLE outreach_messages ADD COLUMN IF NOT EXISTS owner_user_id TEXT;

CREATE INDEX IF NOT EXISTS idx_outreach_owner
    ON outreach_messages(owner_user_id);
