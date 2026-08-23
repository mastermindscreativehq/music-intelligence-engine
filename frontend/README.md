# frontend/

Operator console (Phase 7) for searching, filtering, inspecting and
selecting radio-station intelligence.

**Scope (roadmap Phase 7):** search & filter (location/genre/format +
confidence/status), inspect contacts with confidence and source
attribution visible, select recipients. **Not in scope:** campaign
building, message review/approval, uploads, tracking (Phases 8–10);
nothing here ever sends anything.

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
| `js/basket.js` | Recipient selection store (sessionStorage); export-only payload builder. |
| `js/router.js` | Hash router: `#/` list, `#/station/<key>` detail. |
| `js/views/list.js` | Search/filter form + results table + pagination; "add backend-preferred contacts" per station. |
| `js/views/station.js` | Overview, contacts (confidence bars, reasons, provenance links), submission path (inference-labeled), epistemology notes, verification history with all six statuses. |
| `js/app.js` | Bootstrap: header schema badge from live `/api/v1/health`, basket panel + JSON export, routing. |

## Contract with the backend

- Every view renders LIVE responses from the real Phase 4–6 API. There
  are **no mock fixtures** anywhere; if the backend errors, the envelope's
  `error.code/message` is surfaced to the operator verbatim.
- Interpretation stays server-side: confidence values, preferred contacts,
  verification statuses (`unverified | verified | failed | stale |
  conflicting | unsupported`), fact/inference labels are displayed as the
  backend computed them — the console never re-ranks or promotes.
- Recipient selection is keyed by backend-stable `contact_uid`.

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
