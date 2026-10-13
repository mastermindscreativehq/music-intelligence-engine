-- Song story / creative brief per release + first-class ownership
-- (mirror of SQLite schema v13).
-- ``song_story`` is the operator's own account of the song, used as source
-- material for recipient-specific outreach composition; free text, NULL by
-- default so the writer can honestly report that no story was provided.
-- ``owner_user_id`` records the authenticated uploader; NULL for legacy or
-- anonymous uploads so the historical single-operator behavior is preserved.
-- It is never published in the public track projection.
-- Additive: no existing column or row is touched.

ALTER TABLE tracks ADD COLUMN IF NOT EXISTS song_story TEXT;
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS owner_user_id TEXT;
