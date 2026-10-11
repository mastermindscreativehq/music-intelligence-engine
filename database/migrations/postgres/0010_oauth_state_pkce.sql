-- PKCE code verifier (mirror of SQLite schema v11). google-auth-oauthlib
-- generates the verifier inside Flow.authorization_url() and puts the matching
-- challenge in the authorization URL; the callback's token exchange must replay
-- the SAME verifier or Google answers ``invalid_grant: Missing code verifier``.
-- Storing it on the single-use state row avoids a second (cookie) mechanism and
-- behaves identically for local HTTP and production HTTPS.

ALTER TABLE oauth_state ADD COLUMN IF NOT EXISTS code_verifier TEXT;
