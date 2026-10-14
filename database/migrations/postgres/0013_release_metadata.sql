-- Release metadata per track (mirror of SQLite schema v14).
-- ``title`` and ``artist_name`` are the operator-authored identity of a
-- release; ``genre`` is an optional classification. They let My Music create
-- a release from metadata alone (no binary upload), while the legacy
-- content-addressed blob columns stay intact for existing rows. All three
-- default to NULL, so an existing upload is honestly "untitled" rather than
-- mislabeled. Additive: no existing column or row is touched.

ALTER TABLE tracks ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS artist_name TEXT;
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS genre TEXT;
