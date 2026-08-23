# Enrichment & Verification (Phase 5)

Cross-source comparison, an explicit verification workflow, and the
optional Ollama-backed enrichment layer. Everything here runs **fully
deterministic offline**; the local LLM is an optional reasoning layer that
can only *hint*, never decide alone, and never invent facts.

Epistemology (engine-wide, enforced in code): **FACT ≠ INFERENCE ≠
UNKNOWN**. Facts carry provenance; inferences are labeled with method,
model identifier, and prompt version; unknowns stay unknown.

---

## 1. Source comparison — `enrichment/compare.py`

Read-only reporting over the provenance-backed observations already
stored on an intelligence record. Per claim slot (an email fact, a phone
fact, the submission URL/instructions, a contact's provenance values):

| Outcome         | Meaning                                            |
|-----------------|----------------------------------------------------|
| `corroborated`  | same normalized value seen from ≥ 2 independent domains |
| `conflicting`   | different values claimed for the same slot          |
| `single_source` | exactly one independent source observed the value   |
| `unobserved`    | no provenance-bearing evidence at all               |

Guarantees:

- The input record is **never mutated**.
- Values are compared after normalization (emails lowercased via
  `enrichment.emails.normalize_email`; URLs casefolded, fragment-stripped).
- Independence is measured per **registrable domain** of source URLs.
- Conflicts keep **every side** with verbatim provenance and annotate
  each side with a `strongest_evidence` strength (`SOURCE_STRENGTH`
  ranking: official website pages > mailto / submission pages > contact
  pages > directories > social/search). The ranking exists purely to
  annotate — the comparator **never picks a winner and never overwrites**
  stronger evidence with weaker.

Entry points: `SourceComparator.observe/observe_fact/evaluate` for
fine-grained use; `compare_record(record)` returns
`{"claims": [...], "summary": {...}}`.

## 2. Verification workflow — `enrichment/verify.py`

Turns comparison outcomes into explicit states (extends the documented
entity model "Verification Result: status unverified|verified|failed|stale"
in `docs/data-model.md`):

| Status        | Set when                                                   |
|---------------|------------------------------------------------------------|
| `unverified`  | support exists but not enough to verify (default)          |
| `verified`    | corroborated by ≥ 2 independent sources (`verifier=code`)  |
| `conflicting` | sources disagree; both sides preserved, no auto-winner     |
| `unsupported` | a stored contact email has no provenance reference at all  |
| `stale`       | previously verified but older than the freshness budget    |
| `failed`      | reserved: an explicit attempt contradicted the claim       |

- Freshness budget defaults to 90 days (`--max-age-days`), measured from
  `last_verified_at`.
- Results are append-only dicts: claim, status, method, verifier
  (`code`; `human` is reserved), evidence references (with provenance),
  reasons, `checked_at`.
- `apply_verification` mutates **lifecycle fields only** (`last_verified_at`,
  per-contact `verified_at`, a bounded history under
  `raw_metadata.verification`). Fact values are never rewritten here;
  conflict resolution is deliberately left to humans or newer evidence.
- CLI: `python -m enrichment.verify --input enriched.json [--apply-out out.json]`

## 3. Optional Ollama layer — `enrichment.llm`

Implements the guardrails in `docs/ai-architecture.md`:

1. **Deterministic rules first.** `suggest_contact_role(context_text)`
   returns the rule-based role whenever `enrichment.roles.classify_role`
   can decide — the LLM is consulted strictly for genuinely ambiguous
   snippets.
2. **Strict validation before use.** Responses must parse as JSON and
   satisfy the template's declared output schema; schema violations are
   deterministic rejections and are not retried. Transport errors retry
   up to `max_attempts`, then fail cleanly.
3. **Versioned prompts as files.** Prompts live only under
   `prompts/templates/enrichment/*.v<N>.md` (see `prompts/README.md`);
   Python source contains no prompt text and no secrets. Existing files
   are immutable — changes create new versions.
4. **Auditable results.** Successful hints return metadata:
   `{"kind": "inference", "method": "llm", "model": ..., "prompt_version": ..., "template_hash": ...}`.

Fallback contract (never raises on infrastructure failure):

| Situation                          | Result                    |
|------------------------------------|---------------------------|
| no client passed                   | `("unknown", None)`       |
| server unreachable                 | `("unknown", None)`       |
| unparsable / schema-invalid output | `("unknown", None)`       |
| hint outside `ROLE_VOCABULARY`     | `("unknown", None)`       |

Configuration uses environment variable **names** only — values are read
at runtime and never logged:

- `MIE_OLLAMA_HOST` (default `http://localhost:11434`)
- `MIE_OLLAMA_MODEL` (default `qwen2.5-coder:7b`)

The client speaks stdlib HTTP to the local Ollama API (`/api/tags`
reachability probe, `/api/generate` with `"stream": false`). A transport
callable can be injected for tests/offline runs; the test suite requires
no running Ollama server.

## 4. Opt-in pipeline hook

`EnrichmentEngine(role_advisor=...)` accepts a callable of one string
returning `(role, metadata | None)`. After normal enrichment it consults
the advisor **only for contacts whose role is still `unknown`**, using
the contact's own stored fields as context. A validated hint flips the
role, appends an inference provenance entry
(`kind/method/model/prompt_version/value/observed_at`), and re-scores the
contact with an added reason noting local-model inference. Advisor
failures never kill enrichment. Default engines (`role_advisor=None`)
behave byte-identically to Phase 4.

CLI opt-in: `python -m discovery.radio.enrich --input ... --ai-roles`
(constructs the client lazily; without the flag nothing network-capable
is built).

## 5. Tests

`tests/test_phase5_verification.py` (34 tests, all offline) covers:
comparison matrix (corroboration across independent domains, conflicts
preserving both sides, single-source, empty records, input immutability),
verification transitions (verified/stale/unverified/unsupported, lifecycle
application, summary counts), template contracts (sections, variables,
schemas, examples), strict output validation, retry/fallback behavior,
env-var configuration, rules-first precedence, pipeline hook semantics
(default untouched; inference provenance appended; honest `unknown`;
advisor exceptions isolated), and CLI wiring proving the default run
never constructs a network client.
