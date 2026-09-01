# frontend/

Operator console (Phase 7) for searching, filtering, inspecting and
selecting radio-station intelligence.

**Scope (roadmap Phase 7):** search & filter (location/genre/format +
confidence/status), inspect contacts with confidence and source
attribution visible, select recipients, and start outreach. The page is
**action-first**: Discover → open a station → Find Contacts / Send Music /
Visit Website / Add to Campaign → Start outreach. Recipients are staged in
a basket and reviewed in a minimal draft composer that hands off to the
operator's email client (`mailto:`) — **nothing here ever sends anything**
and there is no server-side campaign engine.

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
| `index.html` | Shell: CSP meta (`default-src 'self'`, `connect-src 'self'`), layout skeleton. |
| `css/app.css` | Dark operator theme. |
| `js/dom.js` | `el()` element builder — the ONLY way DOM is created; dynamic data is attached as text/attributes, never markup. |
| `js/api.js` | Envelope client for `/api/v1/*`; unwraps `ok/data/error`, raises typed `ApiError`. |
| `js/basket.js` | Recipient selection store (sessionStorage); lookup by `contact_uid`. |
| `js/router.js` | Hash router: `#/` list, `#/station/<key>` detail, `#/outreach?recipient=<uid>,<uid>` composer. |
| `js/views/list.js` | Search/filter form + results table + pagination; "add backend-preferred contacts" per station. |
| `js/views/station.js` | Action-first + evidence-backed: Actions bar (Find contacts / Send music / Visit website / Add to campaign), "Useful pages" (discovered public pages surfaced as navigational links), individual recipient cards grouped as **Recommended Contacts** (backend *preferred_for_submissions* or music-outreach roles), **Other Discovered People** (weak evidence, shown for intelligence only), and **Station-Level Contact Routes** (EXACT backend-discovered submission/instructions URLs with reachability), plus submission path (inference-labeled) and a collapsed "Intelligence Details" disclosure (overview, epistemology, verification history with all six statuses, link accessibility). |
| `js/views/outreach.js` | Handoff composer: loads recipients from the basket with station context + evidence source, editable From (operator's own address), subject/body persisted to `sessionStorage`, "Copy message" and "Open in email" (`mailto:`) handoff. |
| `js/app.js` | Bootstrap: header schema badge from live `/api/v1/health`, basket panel with **Start outreach** action, routing. |

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
  The composer only ever hands off to the operator's own email client via
  `mailto:`; nothing is sent by this console.

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
