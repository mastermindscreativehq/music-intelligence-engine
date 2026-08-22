# prompts/

Versioned LLM prompt templates for the Ollama layer (model: `qwen2.5-coder:7b`).

**Conventions** (see [`docs/ai-architecture.md`](../docs/ai-architecture.md)):

- Templates live under `prompts/templates/<domain>/<name>.v<N>.md`.
- Every template declares purpose, input variables, expected structured output
  (JSON schema), and an example.
- Prompt versions are immutable; changes create a new `v<N+1>` file.
- Records in the database reference the prompt version used, keeping results auditable.
- No secrets ever appear in prompt files.

No templates yet — they arrive with Phase 5 (enrichment) and Phase 9 (outreach drafting).
