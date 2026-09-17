# frontend/

Operator console for searching, filtering, inspecting and selecting
radio-station intelligence, and preparing + tracking outreach.

**Scope:** search & filter (location/genre/format + confidence/status),
inspect contacts with confidence and source attribution visible, and run
the canonical outreach flow: Dashboard → Stations → Station (PROFILE /
SUBMISSION INFORMATION / CONTACTS / OUTREACH) → select contacts + a release
→ "Start outreach" creates real outreach RECORDS → hand them off from the
Outreach page → track status on the activity log. The page is
**action-first** with ONE intended action per surface — there is no
parallel "campaign" system and no competing "Reach Out"/"Add to campaign"
verbs. Recipients are staged per station as a working list, then converted
into records (`#/outreach`); handoff is `mailto:` / copy / opening the
station's real submission page — **nothing here ever sends anything**.

## Zero-dependency by design

No framework, no build step, no npm. Plain ES modules served same-origin
by `backend.webapp`:

```
python -m backend.webapp --db path/to/db.sqlite     # UI + API on one origin
```

Rationale: the repository adds dependencies per phase; the sandbox has no
Node runtime; and the operator surface is small enough that vanilla
modules keep the security story simple (strict CSP, no inline handlers,
no third-party code).

## Layout

| File | Role |
| --- | --- |
| `index.html` | Shell: CSP meta (`default-src 'self'`, `connect-src 'self'`), layout skeleton, nav. |
| `css/app.css` | Dark operator theme. |
| `js/dom.js` | `el()` element builder — the ONLY way DOM is created; dynamic data is attached as text/attributes, never markup. |
| `js/api.js` | Envelope client for `/api/v1/*`; unwraps `ok/data/error`, raises typed `ApiError`. |
| `js/basket.js` | Temporary recipient staging store (sessionStorage); lookup by `contact_uid`. |
| `js/router.js` | Hash router: `#/` dashboard, `#/stations` list, `#/station/<key>` detail, `#/tracks` my music, `#/outreach` active records, `#/outreach-history` full ledger. |
| `js/views/dashboard.js` | Overview only: counts, outreach summary, recent records, alerts, quick paths. |
| `js/views/list.js` | Search/filter form + results table + pagination; each row opens the station. |
| `js/views/station.js` | Primary intake — STATION PROFILE / SUBMISSION INFORMATION / CONTACTS ("Add to outreach") / OUTREACH (release selector + Start outreach → creates records), plus useful pages and a collapsed Intelligence Details disclosure. |
| `js/views/outreach.js` | Active outreach records: open station, compose & hand off, log real events (sent/responded/follow_up/failed/closed). |
| `js/views/outreachHistory.js` | Full ledger incl. failed/closed records + append-only attempts. |
| `js/views/outreachRecords.js` | Shared record-card/chip/attempt/detail rendering used by both outreach surfaces. |
| `js/views/outreachModal.js` | Single composer: pick a release, compose, create the record; record mode adds the `opened_in_email` handoff attempt. |
| `js/app.js` | Bootstrap: router wiring + staged-recipient counter. |

## Contract with the backend

- Every view renders LIVE responses from the real Phase 4–6 API. There
  are **no mock fixtures** anywhere; if the backend errors, the envelope's
  `error.code/message` is surfaced to the operator verbatim.
- Interpretation stays server-side: confidence values, preferred contacts,
  verification statuses (`unverified | verified | failed | stale |
  conflicting | unsupported`), fact/inference labels are displayed as the
  backend computed them — the console never re-ranks or promotes.
- Recipient selection is keyed by backend-stable `contact_uid`.
- **Data integrity:** the frontend never constructs an outreach route. A
  person is reachable only when the backend stored the exact email
  (`contact.email`) or URL (`submission.submission_url.value`), and every
  card shows the evidence (`source_url` / provenance) that produced it —
  "No verified outreach route found" is an honest state, never a guess.
- Outreach statuses use the canonical vocabulary
  `ready | sent | responded | follow_up | failed | closed`; a mail-client
  open appends an `opened_in_email` attempt and never changes the stored
  status. `sent` is only earned by a provider-confirmed send.

## Testing

`tests/test_phase7_frontend.py` covers this layer from Python against the
REAL stack: static-asset integrity, XSS-surface scans (no innerHTML /
eval / document.write / inline handlers / remote assets), a coupling test
asserting every API path referenced in JS exists in the served route table
(`backend.routes`) with only supported query parameters, and a live
single-origin integration class (static serving, traversal rejection,
API round-trips incl. verification history).

JavaScript unit tests are deferred until a Node runtime is available in
the development environment; until then the browser-side logic is kept
dependency-free and pinned by the Python contract tests above.
