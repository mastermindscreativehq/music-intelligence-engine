# outreach/

Outreach layer — the canonical outreach RECORD + delivery abstraction.

**Owns:** outreach records (release + station + contact + role + contact method
+ status + history), the append-only attempt ledger, provider capability
abstraction, and status transitions. The canonical unit is a single
outreach record: MUSIC + STATION + CONTACT. There is deliberately NO
"campaign" entity or parallel campaign workflow — bulk labels like
"campaign" were consolidated into records (stabilization 2026-09-08).

**Record lifecycle (canonical):**

    ready -> sent -> responded -> follow_up -> closed     (plus failed)

- `ready`: record prepared from a station page (recipient + optional release).
- Handing a record to your mail client appends an `opened_in_email` ATTEMPT
  to the ledger; it never changes the stored status (still `ready`).
- `sent` is earned ONLY by a provider-confirmed send, never a mail-client
  open. `responded` / `follow_up` / `closed` are operator-logged events.
- Legacy statuses were remapped non-destructively (v6 migration):
  `draft` -> `ready`, `opened_in_email` -> `ready` (the mail-client handoff
  stays verbatim in the attempts ledger).

**Hard rules:**

- No message is ever marked sent unless a provider confirmed delivery; the
  app itself never sends (mailto handoff + copy only, or opening the
  station's real submission form).
- "Email found" never implies "send".
- Every action is appended to the attempt ledger; records are preserved by
  migration, never deleted.
- Release/station/contact/role/method are stored facts, never invented.

No email provider credentials are configured; every concrete provider is a
local/no-op stub. Real providers can be added without touching callers.