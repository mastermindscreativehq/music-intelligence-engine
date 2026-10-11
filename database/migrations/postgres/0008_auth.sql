-- Auth + Google Gmail integration (mirror of SQLite schema v9).
--
-- users: one row per operator account. user_google_accounts: the connected
-- Gmail identity with the OAuth tokens used to send outreach FROM THIS
-- ACCOUNT (never a shared system mailbox); tokens live server-side only and
-- are never returned to the frontend. user_sessions: opaque Bearer session
-- tokens minted at login/connect and validated per request.
-- Timestamps stay TEXT (ISO-8601) exactly like every other table so payloads
-- are byte-identical across backends; access/refresh tokens stay TEXT.

CREATE TABLE IF NOT EXISTS users (
    user_id           TEXT PRIMARY KEY,
    email             TEXT UNIQUE NOT NULL,
    name              TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_login_at     TEXT
);

CREATE TABLE IF NOT EXISTS user_google_accounts (
    id                BIGSERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    google_id         TEXT UNIQUE,
    email             TEXT NOT NULL,
    picture           TEXT,
    access_token      TEXT,
    refresh_token     TEXT,
    token_expiry      TEXT,
    scopes            TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(user_id, email)
);
CREATE INDEX IF NOT EXISTS idx_uga_user ON user_google_accounts(user_id);

CREATE TABLE IF NOT EXISTS user_sessions (
    session_token     TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    revoked_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_us_user ON user_sessions(user_id);