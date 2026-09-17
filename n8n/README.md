# n8n/

Orchestration layer. n8n coordinates workflows between modules but **is not the
application** — it contains no business logic.

**Workflow units (kept deliberately small, never one monolith):**

- `discovery → crawler` (Phase 2)
- `extraction → enrichment → validation → database` (Phases 4–6)
- `select contacts → generate message → human review → approval → send → track`
  (Phases 7–10), with the review/approval step always involving a human.
- **Phase 11 — automated discovery runs** (done, below)

Workflow JSON exports live in `n8n/workflows/`. Local runtime data is
git-ignored (`n8n/data/`).

## Phase 11: automated daily DJ discovery

`workflows/mie-daily-dj-discovery.json` is the production scheduled run. n8n
only triggers, authenticates, and reports — the engine owns every decision
(provider selection, querying, normalization, dedupe, ingestion, run ledger).

### Flow

`Schedule daily (06:00)` → `Build DJ discovery job(s)` →
`POST /api/v1/discovery/jobs` → `Capture run metrics` → `Status: completed?` →
`Run successful (completed)` / `completed_with_failures (preserved)`.

- **Schedule** — cron `0 6 * * *` (daily 06:00).
- **Build job(s)** — a Code node returns one item per discovery job. Add a
  job object to the `jobs` array to fan out to more jobs; the HTTP node runs
  once per item, so no other node changes.
- **HTTP request** — POSTs the job to `{{ $env.MIE_BASE_URL }}/api/v1/
  discovery/jobs` with `Authorization: Bearer {{ $env.MIE_AUTOMATION_TOKEN }}`.
  If the request itself fails (non-200 / network), the node fails and the
  execution surfaces the error.
- **Capture run metrics** — flattens the MIE envelope into one item per job:
  `ok`, `job_type`, `run_id`, `provider`, `queries_run`, `candidates_found`,
  `records_ingested`, `duplicates`, `failures`, `status`, `started_at`,
  `completed_at`.
- **Status branch** — `completed` → success terminal; any other status
  (notably `completed_with_failures`) → the preserved terminal. Both branches
  leave the workflow successful: ingestion already happened server-side, so a
  `completed_with_failures` run is surfaced (visible failure count + status)
  without failing the automation run.

### Secrets

- `SERPAPI_API_KEY` lives ONLY in the MIE backend's environment. n8n never
  sees it — the workflow contains no SerpAPI credentials and never builds a
  SerpAPI URL.
- `MIE_AUTOMATION_TOKEN` is the shared secret for the MIE endpoint, read from
  the n8n process environment via `$env` — never hard-coded in the JSON.

### Required n8n environment variables

| Variable | Purpose |
|---|---|
| `MIE_BASE_URL` | MIE backend root, e.g. `http://127.0.0.1:8788` or `https://your-backend.up.railway.app` (no trailing slash). Change here, not in the workflow, when n8n moves to Docker / another host. |
| `MIE_AUTOMATION_TOKEN` | The same shared token set as `MIE_AUTOMATION_TOKEN` on the MIE backend. Must match, or the endpoint answers 401. |

These must be present in the environment n8n runs in (systemd unit / Docker
`-e` / `.env` loaded by n8n), because the workflow reads them with `$env`.

### Import / setup

1. n8n editor → **Workflows → Import from File** → select
   `n8n/workflows/mie-daily-dj-discovery.json`.
2. Ensure `MIE_BASE_URL` and `MIE_AUTOMATION_TOKEN` are set in the n8n
   process environment.
3. Activate the workflow in the editor.
4. Inspect results in **Executions**; the backend run ledger row is also
   readable at `GET /api/v1/discovery/jobs/{run_id}`.

### Expanding to more jobs

Open **Build DJ discovery job(s)** and add a job object to the `jobs` array,
e.g. a second DJ genre/geography. The HTTP node runs once per item and the
`Capture run metrics` node aggregates all responses.