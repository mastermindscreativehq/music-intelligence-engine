# Radio Enrichment & Intelligence (Phase 3)

Phase 3 turns a discovered `StationRecord` into a **RadioIntelligenceRecord**:
station characteristics, enriched contacts with explainable confidence, and a
submission path an artist could actually use. It consumes Phase 2 output and
adds zero dependencies.

- Pipeline entry: `discovery/radio/enrich_pipeline.py`
- Pure assembly layer: `discovery/radio/intelligence.py`
- New primitives: `enrichment/formats.py`, `enrichment/submissions.py`,
  extended `enrichment/{roles,confidence}.py`, form parsing in
  `crawler/pages.py`
- Output schema additions: `discovery/radio/schema.py` (`EnrichedContact`,
  `SubmissionPath`, `SourceFetchRecord`, `RadioIntelligenceRecord`) and
  `discovery/models.py` (`EnrichmentResult`)
- Tests: `tests/test_enrichment_intelligence.py` (matrix A–T)

## Running it

Offline (default — no network, deterministic):

```powershell
python -m discovery.radio.enrich --input result.json --output enriched.json
```

Opt-in bounded fetching (`robots.txt` respected, per-station URL budget,
per-host rate limiting):

```powershell
python -m discovery.radio.enrich --input result.json --fetch
```

Input is either a DiscoveryResult JSON (`{"records": [...]}`) or a bare
array of StationRecord dicts.

## Data integrity rules

| Rule | Enforcement |
|------|-------------|
| FACT ≠ INFERENCE ≠ UNKNOWN | Facts carry provenance dicts; method inference bundles are labeled `"kind": "inference"` with reasons; absent evidence leaves fields unset |
| Nothing invented | Submission email requires page evidence or an evidenced music-submission role; market only from explicit "serving … area/region/market/community" claims |
| Preserve Phase 2 | Input records are never mutated; existing facts/provenance survive merging; `verified_at` stays None (no verification claims in this phase) |
| Unverified cap | Contact scores clamp at 0.95 — extraction is not verification |

## What gets enriched

1. **Characteristics** — genres (keyword counts, ranked, evidence map),
   broadcast formats (music/talk/news/sports/variety cues), market area.
   Carried `format`/`genres` facts survive and merge with page-derived ones.
   Whitespace is normalized before keyword matching so HTML line-wraps cannot
   hide phrases like `music programming`.
2. **Contacts** — rebuilt from fetched pages via the Phase 2 extractor, then
   unioned with existing contacts by email (provenance appended, never lost).
   Role classification near an address uses `classify_role_near`: same text
   line first, then up to three preceding lines (labels precede addresses),
   then following lines — dense staff pages can't cross-contaminate labels.
3. **Submission path** — dedicated URL fact, evidenced submission email,
   instruction snippet (sentence rules, ≤400 chars), restrictions
   (`no_attachments`, `digital_only`, `postal_only`, `no_phone_calls`,
   `no_drop_ins`, `review_window`), on-page form detection, and inferred
   methods (`email` / `web_form` / `postal`) labeled as inference.
4. **Preferred contact** — only contacts with music-relevant roles
   (`music_director` > `music_submission` > `programming` /
   `music_programmer` > `program_director`) may be flagged
   `preferred_for_submissions`. Generic inboxes (`info@`) are never promoted
   without their own music-role evidence.
5. **Confidence** — station-level score starts from the Phase 2 value plus
   transparent deltas (genre evidence, captured instructions, identified
   submission contact); contact-level scoring is additive and explained:
   source-page weight, own-domain email, named person, music role,
   free-provider penalty, floor 0.05, cap 0.95. Every record carries
   `confidence_reasons[]`.

## Engine behavior

- Offline by default; live mode is explicit (`--fetch` or injected fetcher).
- Fetch targets are priority ordered: submission URL fact → programming →
  contact → website → previously seen sources, capped per station.
- Per-URL failures are recorded as `SourceFetchRecord`s without failing the
  station; per-station exceptions become ledger entries and never kill the run.
- Structured events: `enrichment_started`, `enrichment_page_fetch`,
  `station_enriched`, `submission_path_found`, `enrichment_completed`,
  `enrichment_failed`.

## Deliberate limits

No verification (emails are not pinged), no deliverability claims, no CAPTCHA
or auth bypass, no attachments parsed beyond HTML, radio stations remain the
only target type. Future phases reuse these primitives unchanged.
