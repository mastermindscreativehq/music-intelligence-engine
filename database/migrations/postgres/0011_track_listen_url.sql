-- Persistent official listening URL per release (mirror of SQLite schema v12).
-- The audio blob stays content-addressed and opaque; this is the ONE
-- operator-owned presentation field the outreach handoff needs — where the
-- release can be heard. Editable after upload and NULL by default, so an
-- unset link is honestly absent rather than invented at compose time.
-- Additive: no existing column or row is touched.

ALTER TABLE tracks ADD COLUMN IF NOT EXISTS listen_url TEXT;
