# enrichment/

Contact intelligence layer. Deterministic rules first; Ollama consulted only where
semantic interpretation genuinely helps — see
[`docs/ai-architecture.md`](../docs/ai-architecture.md) for the decision table.

**Never does:** network fetching (→ `crawler/`), persistence decisions (→ backend),
or sending anything.

## Phase 2 status: implemented (deterministic first pass)

| Module          | Responsibility                                                        |
|-----------------|-----------------------------------------------------------------------|
| `emails.py`     | Email extraction (text + mailto), normalization, quality signals. Obfuscation is never defeated; no invented addresses; deliverability never claimed |
| `roles.py`      | Transparent ordered keyword rules → music_director, program_director, programming, music_submission, music_programmer, station_manager, producer, host, dj, media, booking, general, advertising, unknown. `classify_role_near()` adds line-aware classification for dense pages |
| `contacts.py`   | Contact assembly: role-from-context, conservative name adjacency heuristics, phone extraction |
| `stations.py`   | Evidence-based station-type classification (college/community/public/independent/internet/unknown) + social URL detection |
| `dedupe.py`     | Canonical-domain identity keys; union-merges preserving provenance; name-only merging forbidden |
| `confidence.py` | Additive explainable scoring with `confidence_reasons[]`; no opaque scores |

## Phase 3 status: implemented

| Module           | Responsibility                                                          |
|------------------|--------------------------------------------------------------------------|
| `formats.py`     | Genre/format keyword evidence over normalized text (line-wraps can't hide phrases), explicit market-area claims only |
| `submissions.py` | Submission instruction snippets, restriction signals, method inference — always labeled inference |
| `confidence.py`  | + `score_contact()`: explainable contact-level confidence, capped at 0.95 (unverified) |

Future enrichment hook: LLM re-classification of `unknown` roles/types can be
inserted behind the same function signatures without changing call sites.
