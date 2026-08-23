# Music Intelligence Engine

A production-oriented **music industry intelligence & outreach platform**, designed to
discover, enrich, verify, and score legitimate music-industry contacts — starting with
**radio stations** — and to support careful, human-approved music outreach.

> **Current phase: PHASE 5 — ENRICHMENT & VERIFICATION.**
> Phases 1–4 are complete (foundation; radio discovery; website intelligence
> with bounded opt-in fetching; contact extraction; SQLite storage + API).
> Phase 5 adds cross-source comparison, an explicit verification workflow,
> and the optional Ollama-backed enrichment layer with versioned prompts —
> fully deterministic offline; see
> [`docs/enrichment-verification.md`](docs/enrichment-verification.md).
> No crawling of live sites has been performed by this repo yet; no outreach
> exists.

---

## Mission

Build a reusable intelligence platform for music-industry contacts. `radio_station` is the
first target type plugged into the platform; playlist curators, DJs, blogs, publications,
labels, A&R representatives, festivals, and influencers will reuse the same infrastructure.

The core pipeline every target type passes through:

```
DISCOVER → CRAWL → EXTRACT → NORMALIZE → ENRICH → VERIFY → SCORE
        → STORE → SEARCH → PERSONALIZE → APPROVE → OUTREACH → TRACK
```

## Core principles

- **Modular by target type.** The pipeline is generic; radio is implementation #1.
- **Deterministic first.** Normal code does crawling, normalization, validation, storage.
  The local LLM (Ollama) is used only where semantic reasoning adds real value.
- **Intelligence, not scraping.** Every fact keeps its source, extraction method,
  confidence, verification status, and timestamp. *Found* ≠ *verified* ≠ *relevant*.
- **Human in the loop.** No message is ever sent without review and approval.
- **No premature infrastructure.** Dependencies and services are added per phase.

## Directory structure

| Directory     | Responsibility                                                        | Active from |
|---------------|-----------------------------------------------------------------------|-------------|
| `discovery/`  | Reusable discovery subsystem: requests, queries, providers, events    | Phase 2 ✓   |
| `discovery/radio/` | Radio pipeline + enrichment engine + normalized intelligence schema | Phase 2–3 ✓ |
| `crawler/`    | Deterministic HTTP retrieval, URL handling, focused page discovery    | Phase 2 ✓   |
| `enrichment/` | Contact extraction/normalization, classification, dedup, confidence, formats/submissions intelligence | Phase 2–3 ✓ |
| `backend/`    | Application/API layer: organizations, contacts, search, campaigns     | Phase 6     |
| `frontend/`   | UI: search, filter, inspect, select, approve, track                   | Phase 7     |
| `outreach/`   | Campaign preparation, personalized messages, approval-gated sending   | Phase 8–10  |
| `database/`   | PostgreSQL/Supabase schema & migrations                               | Phase 6     |
| `n8n/`        | Workflow orchestration (small logical workflows, never one monolith)  | Phase 2+    |
| `prompts/`    | Versioned LLM prompt templates                                        | Phase 5+    |
| `docs/`       | Architecture, data model, roadmap, AI + radio-discovery docs          | now         |
| `tests/`      | Test foundation (stdlib `unittest`; pytest may be adopted later)      | now         |

Phase history: PHASE 1 — FOUNDATION (architecture/docs/config/test baseline);
PHASE 2 — RADIO DISCOVERY ENGINE; PHASE 3 — RADIO INTELLIGENCE / ENRICHMENT (current).

## Technology stack

| Layer          | Technology                                   | Status      |
|----------------|----------------------------------------------|-------------|
| Intelligence   | Python 3.14                                  | chosen      |
| Local AI       | Ollama `0.32.14`, model `qwen2.5-coder:7b` @ `http://localhost:11434` | configured |
| Backend API    | FastAPI (planned, not installed)             | Phase 6     |
| Database       | PostgreSQL / Supabase-compatible             | Phase 6     |
| Frontend       | Node.js / npm based SPA (framework TBD)      | Phase 7     |
| Orchestration  | n8n                                          | Phase 2+    |

## Local development

Verified environment (Windows 11, PowerShell):

- Python 3.14.0 — `C:\Python314\python.exe`
- Node.js v24.10.0 / npm 11.6.1
- Git 2.53.0 · Docker 29.6.2
- Ollama 0.32.14 listening on `localhost:11434`

Run the Phase 1 test suite (zero external dependencies required):

```powershell
python -m unittest discover -s tests -v
```

Configuration lives in `.env` (create it from `.env.example`). `.env` is git-ignored;
`.env.example` contains placeholders only.

## Roadmap

See [`docs/roadmap.md`](docs/roadmap.md). Summary:

1. Foundation ✓
2. Radio Discovery ✓
3. Radio Website Intelligence ← current
4. Contact Extraction
5. Enrichment & Verification
6. Database/API
7. Frontend
8. Music Submission
9. Personalized Outreach
10. Outreach Tracking
11. Expansion (curators, DJs, blogs, labels, A&R, festivals, influencers)

## Documentation

- [Architecture](docs/architecture.md) — system boundaries, data flow, module responsibilities
- [Radio Discovery Engine](docs/radio-discovery.md) — Phase 2 pipeline, retrieval rules, provider abstraction
- [Radio Enrichment](docs/radio-enrichment.md) — Phase 3 enrichment engine, intelligence records, submission paths
- [Data Model](docs/data-model.md) — entities, radio mapping, dedup strategy, confidence semantics
- [AI Architecture](docs/ai-architecture.md) — Ollama role, deterministic-vs-AI rules, prompt conventions

## Running the discovery engine (Phase 2)

Discovery runs against local seed files (legitimate public source exports).
No credentials or live search APIs are involved:

```powershell
python -m discovery.radio.pipeline --request request.json --seed seeds.json
```

`request.json` example: `{"query": "independent radio stations",
"state_or_region": "New York", "limit": 25}`. Output: one JSON document with
normalized station records, per-fact provenance, confidence reasons, and a
failure ledger. See `docs/radio-discovery.md`.

## Running the enrichment engine (Phase 3)

Enrichment consumes discovery output and is **offline by default** — it
re-assembles facts without touching the network:

```powershell
python -m discovery.radio.enrich --input result.json --output enriched.json
```

Add `--fetch` to opt in to bounded live page reads (robots.txt respected,
per-station URL budget, rate limiting). Output adds genres, formats, market
area, enriched contacts with explainable confidence, and an evidenced
submission path (instructions, restrictions, inferred methods labeled as
inference). See `docs/radio-enrichment.md`.
