# prompts/

Versioned LLM prompt templates for the Ollama layer (model: `qwen2.5-coder:7b`).

**Conventions** (see [`docs/ai-architecture.md`](../docs/ai-architecture.md)):

- Templates live under `prompts/templates/<domain>/<name>.v<N>.md`.
- Every template declares purpose, input variables, expected structured output
  (JSON schema), and an example.
- Prompt versions are immutable; changes create a new `v<N+1>` file.
- Records in the database reference the prompt version used, keeping results auditable.
- No secrets ever appear in prompt files.

Current templates (Phase 5 enrichment, loaded by `enrichment.llm.load_template`):

- `templates/enrichment/contact_role.v1.md` — classify one ambiguous
  contact snippet into the fixed role vocabulary; output
  `{"role": ..., "reason": ...}`.
- `templates/enrichment/station_genre.v1.md` — extract lowercase
  genre/format keywords from one page excerpt; output
  `{"genres": [...]}`.

Outreach drafting templates arrive with Phase 9.
