# Task report — per-contact outreach intelligence

Date: 2026-09-08 · Branch `repair/music-engine-simple` · Commit `2bcd52d`

The console now distinguishes *who to reach* (a contact) from *how to reach
them* (a route). A named decision-maker without their own verified email stays
a first-class, actionable outreach target whenever an official station route
exists. Verified end-to-end in headless Chrome against the live engine for
KEXP, WFMU and WXYC, with no "not reachable" for any station.

## 1. What was actually broken

The per-contact actionability decision was gated purely on `verifiedEmail()`.
The key-contact card and `isActionable()` rendered
"No verified outreach route found" / "…not reachable" for any contact without
a personal email, even when that same person was obviously reachable through
the station's verified submission route. The backend `P2` contact-route build
had the same email/phone bias, so the "Start Outreach" flow silently stalled
for exactly the contacts that matter most — the station's music decision-maker.

## 2. Files changed

- `backend/outreach_intel.py` — per-contact relevance, route pool and best
  route (new, station-independent).
- `backend/contracts.py` — `_annotate_contact_routes`; both `contacts_payload`
  and `intelligence_payload` now expose `relevance`, `outreach_routes`,
  `best_outreach_route`, `can_add_to_campaign` on every contact.
- `frontend/js/views/station.js` — key-contact card rewrite; a per-contact
  "Add to campaign" (including email-less decision-makers); reachable status
  text; relevance chip; best-route + available-routes list; `addAllToCampaign`
  now stages every actionable key contact.
- `frontend/js/views/outreach.js` — per-recipient "Reach out" (verified email →
  composer), "Open route" (submission/contact/DJ page), "Queue" (writes a
  draft to the outreach activity ledger), and an activity-log link.
- `frontend/css/app.css` — relevance chips, route chips, queue-status styling.
- `tests/test_phase11b_contact_actionability.py` — 16 read-path tests.

## 3. What Start Outreach now does

1. **Station page** ranks published contacts by backend role relevance and the
   preferred flag, and shows each decision-maker with their own verified
   email *or* the station's official routes (relevance chip, reachable status,
   best route, available routes).
2. **Add to campaign / Add all** stages recipients with their exact route
   evidence (verified email XOR submission/contact URL — never both, never
   invented).
3. **Outreach list** offers the real hand-off for each recipient: compose
   (email), open the station's own page/form (webform/contact/DJ), or queue.
4. **Queue** creates a `draft` record in the existing outreach ledger
   (POST `/api/v1/outreach`; `create_outreach` requires email XOR
   submission_url, matching the same invariant).
5. **Activity log** (`#/outreach-history`) shows the queued draft; nothing is
   marked `sent` unless a provider confirms delivery.

## 4. How no-email contacts are handled

Separate contact from route per a 6-level hierarchy the UI presents verbatim:

1. verified direct email · 2. official submission route · 3. contact page/form
· 4. department/programming contact page · 5. DJ/program directory · 6. no
route → **marked for further investigation** (honest, never "not reachable").

Only a sendable route flips `can_add_to_campaign` (verified email or an
http(s) station route in `VERIFIED`/`ACTIONABLE` state). A published phone is
shown as channel evidence but is not app-sendable. A route-less person is
clearly "marked for further investigation", not quietly dropped.

## 5. Why KEXP differed from WFMU/WXYC

Not a broken pipeline — different published evidence:

- **KEXP** publishes real decision-maker emails (`md@`, `dj@`, `feedback@` on
  https://kexp.org/contact) → every relevant contact had a verified email, so
  nothing looked unreachable.
- **WFMU** publishes named people without emails (Jessica Romoff, music
  director; Ken Freedman, program director) plus a *verified* `sendmusic`
  webform. The old email-only rule made these named decision-makers appear
  unreachable; the fix routes them to the station's verified submission page.
- **WXYC** publishes no contacts at all and only a contact page — the station
  card now surfaces that page as the real route with zero "not reachable".

## 6. Recurrence prevention

- The route model (`contact_outreach_routes` / `best_contact_outreach_route`)
  is station-independent and tested (`tests/test_phase11b_contact_actionability.py`,
  16 tests: relevance, hierarchy order, reserved-TLD emails never treated as
  verified own routes, dedupe, honest no-route case).
- The UI reads `best_outreach_route`/`can_add_to_campaign` annotations from the
  API; it never re-derives reachability from email. "Not reachable" copy is
  gone from the render path; the remaining honest low state is "marked for
  further investigation".
- A queued outreach never fabricates channels: the queue payload reuses the
  same evidened route fields, and `outreach/service.py` rejects email+URL and
  email-less+URL-less payloads.