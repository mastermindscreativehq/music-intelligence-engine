# n8n/

Orchestration layer. n8n coordinates workflows between modules but **is not the
application** — it contains no business logic.

**Planned workflow units (kept deliberately small, never one monolith):**

- `discovery → crawler` (Phase 2)
- `extraction → enrichment → validation → database` (Phases 4–6)
- `select contacts → generate message → human review → approval → send → track`
  (Phases 7–10), with the review/approval step always involving a human.

Workflow JSON exports will live here from Phase 2 onward. Local runtime data is
git-ignored (`n8n/data/`). No production workflows exist in Phase 1.
