"""Music submission assets (Phase 8).

Owns everything Phase 8 was scoped to — and nothing beyond it:

- ``validation``: pure MP3 upload validation (magic bytes, size ceiling,
  filename sanitization);
- ``storage``: swappable content-addressed blob stores (local filesystem
  today; opaque keys only cross this boundary, never paths);
- ``links``: extraction of submission/reference link targets and SSRF
  guards for accessibility checking;
- ``service``: orchestration shared by every API server adapter.

Phase 8 NEVER sends anything: no email, no form submission, no delivery
events, no campaign state (Phases 9-10 consume this domain instead of the
other way around). The database/API surface deals exclusively in opaque
asset identifiers (``track_id = 'sha256:<hex>'``) so the storage backend
can later move to Supabase/object storage without a contract change.
"""
