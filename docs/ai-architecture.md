# AI Architecture (Phase 1 documentation)

## 1. Role of AI

Ollama is the **local AI reasoning layer**:

- Endpoint: `http://localhost:11434`
- Primary model: `qwen2.5-coder:7b`
- Configured via OpenCode; provider configuration must not be changed without strong
  reason. No cloud providers in this phase.

The system must **not depend on AI for everything**. Deterministic code is preferred
wherever it is reliable.

## 2. Deterministic-vs-AI decision rule

| Task                                        | Approach                    | Why |
|---------------------------------------------|-----------------------------|-----|
| HTTP fetching, retries, rate limiting       | deterministic code          | reliability, compliance |
| Email/phone/domain normalization            | deterministic code          | exact rules exist |
| Format validation (RFC-style email checks)  | deterministic code          | testable, fast |
| Exact dedup on normalized keys              | deterministic code          | precise |
| Contact-name extraction from clear markup   | parser first                | cheap and reliable |
| Ambiguous contact-role inference            | LLM                         | needs semantics ("music director" vs generic inbox) |
| Station-format / genre classification       | rules + LLM fallback        | taxonomy lookup where possible, LLM for messy text |
| Relevance scoring for outreach fit          | LLM-assisted + human review | judgment call; never fully automated |
| Personalized message generation             | LLM draft + human approval  | quality requires both machine drafting and human sign-off |
| Summarization of long pages                 | LLM                         | compression of unstructured text |

Rule of thumb: **if the same input should always yield the same output, write code;
reserve the LLM for interpretation, classification of ambiguity, and language
generation.**

## 3. Guardrails

1. The LLM is never a network actor: it does not crawl, fetch, or send anything.
2. Every LLM response is parsed and validated by deterministic code before use;
   malformed output is rejected and retried with a bounded attempt count.
3. LLM-produced confidence is treated as a signal, combined with rule-based evidence,
   never as ground truth.
4. All AI-derived facts are stored with `method`, model identifier, and prompt version.
5. Prompts contain no secrets and no credentials.

## 4. Prompt management (`prompts/`)

Conventions established now, used from Phase 5 onward:

- Templates live as versioned files under `prompts/templates/<domain>/<name>.v<N>.md`.
- Each template declares: purpose, input variables, expected output format (structured
  JSON schema), and an example.
- Prompt versions are referenced by enrichment/message records so results remain
  reproducible and auditable.
- Changes to a prompt create a new version file; existing versions are never edited in
  place.

No production templates exist yet — they arrive with Phase 5 (enrichment) and Phase 9
(outreach drafting).

## 5. Future AI task inventory

Semantic extraction · classification (org type, station format, genre) · contact-role
inference · relevance scoring · normalization assistance · personalized message
generation · summarization.

Each becomes a small, isolated function behind a stable interface so the local model can
be upgraded or swapped without touching pipeline logic.
