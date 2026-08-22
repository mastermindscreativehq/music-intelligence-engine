# backend/

Application/API layer (Phase 6+).

**Owns:** organizations, contacts, sources, intelligence records, search and filtering,
scoring exposure, campaigns, submissions, outreach state, tracking queries.

**Does not own:** crawler-specific logic (→ `crawler/`), classification rules
(→ `enrichment/`), message generation or delivery (→ `outreach/`), orchestration
(→ `n8n/`).

Planned stack: Python + FastAPI against PostgreSQL/Supabase. Not implemented in Phase 1.
