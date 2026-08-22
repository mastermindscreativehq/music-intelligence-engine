# Implementation Roadmap

## Phase 1 — Foundation ← CURRENT
Architecture, repository structure, documentation, configuration, test harness.
**Deliverables:** module directories, architecture/data-model/AI/roadmap docs,
`.gitignore`, `.env.example`, stdlib test suite validating the foundation.

## Phase 2 — Radio Discovery
Discover radio stations from legitimate public sources. Discovery queue, source
registry, first n8n discovery workflow. No contact extraction yet.

## Phase 3 — Radio Website Intelligence
Crawl station websites; identify contact/submission pages; crawl state, rate limiting,
retry policy; store raw page records with provenance.

## Phase 4 — Contact Extraction
Deterministic extraction and normalization of emails, phones, social links, submission
instructions; contact/submission page parsing.

## Phase 5 — Enrichment & Verification
Classification (org type, format, genre, role), confidence scoring, duplicate detection,
source comparison, verification workflow; first Ollama-backed enrichment steps with
versioned prompts.

## Phase 6 — Database/API
PostgreSQL/Supabase schema + migrations; FastAPI backend exposing organizations,
contacts, sources, confidence data.

## Phase 7 — Frontend
Search, filter (location/genre/format), inspect contacts with confidence and source
attribution, select recipients.

## Phase 8 — Music Submission
MP3 upload, safe storage, accessible submission/reference links.

## Phase 9 — Personalized Outreach
AI-drafted personalized messages from versioned templates, human review and approval
gate, approved-send pipeline.

## Phase 10 — Outreach Tracking
Delivery events, replies, bounces, campaign analytics, suppression and opt-out handling.

## Phase 11 — Expansion
Reuse the same infrastructure for playlist curators, DJs, blogs, publications, labels,
A&R, festivals, events, influencers, creators, and other music-industry entities.
