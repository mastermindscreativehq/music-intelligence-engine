# Stabilization Report — Music Intelligence Engine

**Date:** 2026-09-08
**Status:** feature freeze lifted only after stabilization verified end-to-end.

## What existed

Single-page app (vanilla JS + hash router, no framework):

- **Pages:** `#/` station list, `#/station/<key>` station page, `#/tracks` submission assets, `#/outreach[?recipient=]` outreach working list (basket), `#/outreach-history` persisted outreach ledger.
- **Header nav:** brand/Stations → `#/`, "My music" → `#/tracks`, "Outreach" → `#/outreach-history`, basket badge "outreach list: N".
- **Backend:** FastAPI on 127.0.0.1:8788 (static + API) with API gateway (8787) and ingest worker; **sqlite v5** (`data/music-intelligence.db`): 3 stations (kexp/wfmu/wxyc), 14 contacts, 2 outreach drafts, empty attempt ledger.
- **Outreach workflow:** station list → "Send music" / "Add to campaign" → basket → composer (`Reach out` / `Open route` / `Queue`) → draft appended to activity log; station cards support per-contact "Reach Out" (email) and "Add to campaign" (named contact); a station with zero named contacts falls back to a verified station-published URL as a `webform` recipient.

## What was broken (the four reported problems)

| # | Problem | Root cause |
|---|---------|-----------|
| 1 | Outreach navigation unclear | Header "Outreach" pointed at `#/outreach-history` (the ledger) while the READMEs document "Outreach" as the compose workflow. Worse: bare `#/outreach` matched no router pattern and **silently rendered the stations list**. |
| 2 | Actions scattered / context lost | The history "details" link built `href: "#" + r.links.self` → `#/api/v1/outreach/<id>` → matched no route → user landed on the stations list (context destroyed). Outreach empty view lacked the activity-log link that the populated view had. |
| 3 | Stations inconsistent | All three stations render the same action grid/contact cards. Only gap: URL-only (webform) station recipients were staged **without** `route_kind`/`route_label`, so their labels fell back to generic text instead of the real page. |
| 4 | Dashboard not a random action dump | No dashboard exists; `#/` is a coherent station list. Nothing to fix. |

## What was changed (root-cause repairs only, no new features)

1. **router.js** — pattern `^#\/outreach(?:\?(.*))?$`: bare `#/outreach` now renders the outreach working list; `#/outreach-history` is still matched first.
2. **index.html** — header "Outreach" now → `#/outreach`, matching the README contract (one nav item, one purpose; ledger is reached from within the outreach workflow).
3. **outreachHistory.js** — replaced the broken hash-"details" link with an **inline detail toggle** that fetches `GET /api/v1/outreach/{id}` and renders the record's kv pairs + attempt ledger in place. No new page/route; reuses the existing endpoint.
4. **outreach.js** — empty state now links to the activity log (same link as the populated view).
5. **station.js** — `fallbackStationRoute` returns a `kind`; `webformCampaignRecipient` stages `route_kind`/`route_label` so URL-only recipients are labeled with their real page (verified by check/contact category).
6. **app.css** — `.outreach-detail-slot` styling for the inline record expansion.

Backend code was **not** touched in this phase.

## What was intentionally NOT changed

- **`list.js` `resolveBestRoute`** — re-implements backend route priority on the frontend (duplication). Functionally correct today; swapping it risks subtle behavior change, so it was left and documented.
- **`build_outreach_routes` in contracts.py** — computed multiple times per request (performance nit only; no functional defect). Left to avoid signature churn.
- **`renderOutreachView` `uids` param** — dead (harmless); the basket is the single source of truth. Kept for deep-link/parameter compatibility.
- **Two draft ledger records** (`om_eb1a93d51b984621`, `om_6e8de4e3ee45443c`) — left as demonstration artifacts.
- **Station "Add to campaign" / "Reach Out" semantics** — already coherent; no changes needed.

## Workflows tested end-to-end (headless Chrome, fresh profile)

- Header "Outreach" → outreach working list (not history, not station list).
- Bare `#/outreach` → outreach list (not the stations list).
- Empty outreach list → activity-log link present.
- KEXP "Reach Out" (md card) → composer modal with `md@kexp.org`, verified route, readonly recipient.
- WFMU "Add to campaign" (Romoff card) → email recipient (`route_kind: email`) in outreach list with route + Queue.
- WXYC add-to-campaign fallback → URL recipient staged with `route_kind: contact`, `route_label: Contact`, `https://wxyc.org/contact`.
- Outreach list shows each station's real route; queue available.
- History "details" expands a real draft inline (24 kv fields, attempt ledger), **hash stays** on `#/outreach-history`.
- KEXP / WFMU / WXYC station pages render the same action grid and contact cards with no false "not reachable" / "no route" labels.
- No page JS exceptions or console errors across the whole run.

**Result: 18/18 checks PASS.**

Unit verification: `test_phase7_frontend`, `test_phase9_outreach`, `test_phase11_outreach_intel`, `test_phase11b_contact_actionability` → **98 tests OK**. All four changed JS modules pass strict ESM `node --check` (copied as `.mjs`).

## Remaining ambiguity

- The `?recipient=` param is parsed and passed by the router but unused by the outreach view; this preserves deep links while the basket remains canonical.
- The frontend/backend route-priority duplication stays until a future refactor can agree on one implementation; the backend is the source of truth.
- The history ledger is no longer behind the top-level "Outreach" nav item (by design, per the README); it is reachable from the outreach list and the queue flow.