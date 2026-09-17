# opportunity/

Phase 3: deterministic, explainable music-opportunity intelligence.

**Owns:** the computed pairing of a RELEASE (track record) with a STATION
(intelligence on record) → a 0..100 opportunity score, a HIGH/MEDIUM/LOW tier,
a transparent reason breakdown, a recommended contact + outreach route, and
honest reachability. All derivation is READ-ONLY: computing an opportunity
never writes storage, never sends, and never auto-creates outreach records.

**Modules:**

- `scoring` — the pure weight model (weights sum to 1.0). Every component
  (route, contact, genre, record, identity, format, location, release) is
  computed from recorded evidence only; missing evidence lowers a score and
  is always named. Tiers: HIGH >= 70, MEDIUM >= 40, LOW < 40.
- `service` — repository orchestration: builds outreach routes via the Phase
  XI evidence model (`backend/outreach_intel`), picks the best music contact
  and verified route, checks the existing outreach ledger for duplicates,
  ranks/filters/paginates, and shapes the API contract.

**Reuse (no competing sources of truth):**

- `backend/outreach_intel` — route hierarchy P1..P4, evidence states, contact
  relevance, station levels.
- `discovery.radio.contract.derive_location_status` — honest location status
  (location is displayed, never invented).
- `backend.contracts.station_summary` — canonical station projection.
- `outreach/service` + `frontend/js/views/outreachModal.js` — the single
  composer used by "Prepare outreach".

**Duplicates & history:** if the release was already sent to a station, the
opportunity is marked `already_contacted` with the relevant ledger rows made
visible and its score reduced by a bounded penalty — history is never
deleted, overwritten, or fabricated, and the engine never creates a record on
its own.