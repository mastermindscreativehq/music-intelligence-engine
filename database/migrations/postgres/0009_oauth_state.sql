-- OAuth CSRF state (mirror of SQLite schema v10). See the SQLite comment for
-- the full rationale: single-use, short-lived, bound to the operator who
-- started the flow; anonymous starts bind NULL so the callback can mint their
-- user + session after Google confirms the email.

CREATE TABLE IF NOT EXISTS oauth_state (
    state          TEXT PRIMARY KEY,
    user_id        TEXT REFERENCES users(user_id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    consumed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_state_user ON oauth_state(user_id);