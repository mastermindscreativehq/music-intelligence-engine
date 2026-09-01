"""Systematic end-to-end workflow test for the Music Intelligence Engine.

Exercises the CORE value loop against a LOCAL scratch database — never the
production Railway/Supabase store:

    Stage 2  enrich   : turn seed-station discovery records into sources,
                        named-person contacts with roles/provenance, and a
                        submission path (bounded, read-only live fetching).
    Stage 3  ingest   : POST the enriched records to /api/v1/ingest on an
                        in-process FastAPI app backed by a scratch SQLite DB.
    Stage 4  stations : GET /api/v1/stations (+ detail, filters).
    Stage 5  intel    : GET /api/v1/stations/{key}/intelligence (facts,
                        epistemology, provenance relationships).
    Stage 6  contacts : GET /api/v1/stations/{key}/contacts (contact views,
                        preferred-submission derivation). Also /runs/{id},
                        idempotent re-ingest, and 404 behavior.

SAFETY
- All writes go to a temporary SQLite file created under the OS temp dir
  and deleted on exit. The live Railway/Supabase database is NEVER touched.
- The only network activity is bounded, read-only fetching of the seed
  stations' own public websites via the enrichment engine (robots.txt
  respected, per-station URL budget and per-host rate limit).
- A live-API dry-run target is supported but DISABLED by default: pass
  --live-base-url AND --confirm-live to attempt it (still requires the
  write to be explicitly authorised; it is NOT exercised in CI).

Run:  python scripts/test_e2e_workflow.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# Ensure project root is on sys.path.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Load .env so MIE_* vars are visible WITHOUT touching the live DB (the
# scratch harness uses an explicit local SQLite path, so this never
# connects to Postgres).
_env_path = os.path.join(_ROOT, ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SEED_DISCOVERY = os.path.join(_ROOT, "data", "discovery-result.json")


class Report:
    """Collects endpoint results for a readable pass/fail summary."""

    def __init__(self) -> None:
        self.checks: list[tuple[str, str, bool]] = []

    def add(self, endpoint: str, detail: str, ok: bool) -> None:
        self.checks.append((endpoint, detail, ok))

    def summary(self) -> str:
        lines = []
        passed = sum(1 for _, _, ok in self.checks if ok)
        for endpoint, detail, ok in self.checks:
            mark = "PASS" if ok else "FAIL"
            lines.append(f"  [{mark}] {endpoint} :: {detail}")
        lines.append(f"\n  Total: {len(self.checks)} checks, "
                     f"{passed} passed, {len(self.checks) - passed} failed")
        return "\n".join(lines)


def load_seed_records() -> list[dict]:
    """Read the Phase-2 discovery output that drives the workflow."""
    with open(SEED_DISCOVERY, encoding="utf-8") as handle:
        data = json.load(handle)
    records = data.get("records", data) if isinstance(data, dict) else data
    if not records:
        raise SystemExit(f"no records in {SEED_DISCOVERY}")
    return records


def stage2_enrich(records: list[dict], report: Report,
                  callback=None) -> list[dict]:
    """Stage 2: live enrichment (read-only site fetching)."""
    from discovery.radio.enrich_pipeline import EnrichmentEngine, EngineConfig

    cfg = EngineConfig(max_pages_per_station=4, timeout_seconds=10,
                       rate_limit_seconds=1.0, respect_robots=True)
    engine = EnrichmentEngine(config=cfg)
    engine.set_live()
    result = engine.enrich_records(records)

    enriched = list(result.records)
    report.add("STAGE-2 enrich", f"enriched {len(enriched)} records, "
               f"{result.failure_count} failures", result.record_count > 0)
    for failure in result.failures:
        report.add("STAGE-2 enrich", f"failure: {failure.to_dict()}", False)
    if callable(callback):
        callback(enriched)
    return enriched


def stage3_ingest(client, enriched: list[dict], report: Report,
                  source: str) -> str:
    """Stage 3: POST enriched records to /api/v1/ingest; returns run_id."""
    resp = client.post("/api/v1/ingest",
                       json={"records": enriched, "source": source})
    body = resp.json()
    ok_shape = (resp.status_code == 200 and body.get("ok") is True
                and body.get("error") is None
                and isinstance(body.get("data"), dict))
    report.add("POST /api/v1/ingest",
               f"status={resp.status_code}, data={body.get('data')}", ok_shape)
    data = body.get("data") or {}
    report.add("POST /api/v1/ingest",
               f"records_accepted={data.get('records_accepted')}, "
               f"stations_upserted={data.get('stations_upserted')}, "
               f"contacts_upserted={data.get('contacts_upserted')}, "
               f"submissions_stored={data.get('submissions_stored')}",
               data.get("records_failed") == 0
               and data.get("records_accepted") == len(enriched))
    return data.get("run_id")


def stage4_stations(client, identity_keys: list[str], report: Report) -> None:
    """Stage 4: list + detail + filters."""
    resp = client.get("/api/v1/stations")
    body = resp.json()
    data = body.get("data") or {}
    stations = data.get("stations") or []
    ok = (resp.status_code == 200 and body.get("ok") is True
          and data.get("total") == len(stations))
    detail = f"total={data.get('total')}, returned={len(stations)}"
    report.add("GET /api/v1/stations", detail, ok)
    if stations:
        first = stations[0]
        for field in ("identity_key", "identity_kind", "name",
                      "organization_type", "website", "domain",
                      "confidence_score", "status"):
            report.add("GET /api/v1/stations",
                       f"summary['{field}'] present",
                       field in first)
        links = first.get("links") or {}
        report.add("GET /api/v1/stations",
                   "summary['links'] has intelligence/contacts/self",
                   all(k in links for k in ("self", "intelligence",
                                            "contacts")))

    # Filter by q=WFMU (known seed).
    q = client.get("/api/v1/stations", params={"q": "WFMU"})
    qdata = q.json().get("data") or {}
    any_wfmu = any("WFMU" in (s.get("name") or "") for s in qdata.get("stations") or [])
    report.add("GET /api/v1/stations?q=WFMU",
               f"total={qdata.get('total')}, wfmu_present={any_wfmu}",
               q.status_code == 200 and any_wfmu)

    # Detail for each seed key.
    for key in identity_keys:
        if not key:
            continue
        resp = client.get(f"/api/v1/stations/{key}")
        body = resp.json()
        st = body.get("data") or {}
        report.add(
            f"GET /api/v1/stations/{key}",
            f"status={resp.status_code}, name={st.get('name')!r}, "
            f"confidence={st.get('confidence_score')}",
            resp.status_code == 200 and st.get("identity_key") == key)


def stage5_intelligence(client, key: str, wfmu: bool, report: Report) -> dict:
    """Stage 5: full intelligence view + epistemology + relationships."""
    resp = client.get(f"/api/v1/stations/{key}/intelligence")
    body = resp.json()
    data = body.get("data") or {}
    ok = resp.status_code == 200 and body.get("ok") is True
    report.add(f"GET /api/v1/stations/{key}/intelligence",
               f"status={resp.status_code}", ok)
    for field in ("station", "emails", "phone_numbers", "contacts",
                  "submission", "fetches", "epistemology"):
        report.add(f"...intelligence[{field}] present", "", field in data)
    epi = data.get("epistemology") or {}
    for field in ("facts_count", "inferred_fields", "unknown_fields"):
        report.add(f"...epistemology[{field}] present", "", field in epi)

    contacts = data.get("contacts") or []
    named = [c for c in contacts if str(c.get("name") or "").strip()]
    if wfmu:
        report.add(
            f"...intelligence[kexp-like named contacts]",
            f"{len(named)} named contacts from WFMU enrichment",
            len(named) >= 2)
        for c in contacts:
            report.add(
                f"...intelligence[contact fields]",
                f"name={c.get('name')!r} role={c.get('role')!r} "
                f"confidence={c.get('confidence_score')}",
                "name" in c and "role" in c and "confidence_score" in c
                and isinstance(c.get("provenance"), list))
    return data


def stage6_contacts(client, key: str, report: Report) -> dict:
    """Stage 6: contacts view + preferred-submission derivation."""
    resp = client.get(f"/api/v1/stations/{key}/contacts")
    body = resp.json()
    data = body.get("data") or {}
    ok = (resp.status_code == 200 and body.get("ok") is True
          and data.get("station_identity_key") == key)
    report.add(f"GET /api/v1/stations/{key}/contacts",
               f"status={resp.status_code}", ok)
    views = data.get("contacts") or []
    report.add(f"...contacts[station_name]", data.get("station_name"), bool(
        data.get("station_name")))
    for view in views:
        for field in ("contact_uid", "name", "role", "email", "identity_state"):
            report.add(f"...contact[{field}] present",
                       f"contact_uid={view.get('contact_uid')}", field in view)
    return data


def stage_runs(client, run_id: str, report: Report) -> None:
    """GET /api/v1/runs/{run_id} ledger round-trips."""
    resp = client.get(f"/api/v1/runs/{run_id}")
    body = resp.json()
    data = body.get("data") or {}
    ok = (resp.status_code == 200 and body.get("ok") is True
          and data.get("run_id") == run_id)
    report.add(f"GET /api/v1/runs/{run_id}",
               f"source={data.get('source')}, accepted={data.get('records_accepted')}",
               ok)
    missing = client.get("/api/v1/runs/no-such-run")
    mbody = missing.json()
    report.add("GET /api/v1/runs/{unknown}",
               f"status={missing.status_code}, code={mbody.get('error', {}).get('code')}",
               missing.status_code == 404
               and mbody.get("error", {}).get("code") == "run_not_found")


def stage_idempotent(client, enriched: list[dict], report: Report) -> None:
    """Re-ingesting identical records must not multiply stations/contacts."""
    before = client.get("/api/v1/stations").json()["data"]["total"]
    known_contacts_before = {}
    for s in client.get("/api/v1/stations").json()["data"]["stations"]:
        ik = s["identity_key"]
        known_contacts_before[ik] = len(
            client.get(f"/api/v1/stations/{ik}/contacts").json()["data"]["contacts"])
    resp = client.post("/api/v1/ingest",
                       json={"records": enriched, "source": "e2e-idempotent"})
    data = resp.json().get("data") or {}
    after_total = client.get("/api/v1/stations").json()["data"]["total"]
    contact_count_unchanged = True
    for s in client.get("/api/v1/stations").json()["data"]["stations"]:
        ik = s["identity_key"]
        after = len(client.get(f"/api/v1/stations/{ik}/contacts").json()["data"]["contacts"])
        if known_contacts_before.get(ik) != after:
            contact_count_unchanged = False
    report.add("idempotent re-ingest",
               f"stations before/after={before}/{after_total}, "
               f"contacts unchanged={contact_count_unchanged}, "
               f"accepted={data.get('records_accepted')}",
               after_total == before and contact_count_unchanged)


def stage_404s(client, report: Report) -> None:
    """Server returns 404 envelopes for unknown station keys."""
    resp = client.get("/api/v1/stations/domain:does-not-exist.example")
    body = resp.json()
    report.add("GET /api/v1/stations/{unknown}",
               f"status={resp.status_code}, code={body.get('error', {}).get('code')}",
               resp.status_code == 404
               and body.get("error", {}).get("code") == "station_not_found")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python scripts/test_e2e_workflow.py",
        description="Systematic end-to-end workflow test (scratch SQLite, "
                    "no live writes).")
    parser.add_argument("--keep-db", action="store_true",
                        help="keep the scratch SQLite file on disk")
    parser.add_argument("--seed", default=SEED_DISCOVERY,
                        help="path to discovery output JSON to enrich")
    parser.add_argument("--max-pages", type=int, default=4,
                        help="per-station enrichment page budget")
    parser.add_argument("--live-base-url", default=None,
                        help="(opt-in, DISABLED) live API base URL for a "
                             "dry-run against the ENTIRE deployed server")
    parser.add_argument("--confirm-live", action="store_true",
                        help="required alongside --live-base-url to authorise "
                             "any live write endpoint")
    args = parser.parse_args(argv)

    if args.live_base_url and not args.confirm_live:
        parser.error("--live-base-url requires --confirm-live; live writes "
                     "are refused by default")

    report = Report()
    tmp = tempfile.TemporaryDirectory(prefix="mie_e2e_")
    try:
        db_path = os.path.join(tmp.name, "scratch.sqlite")

        from fastapi.testclient import TestClient
        from backend.app import create_app
        from database.service import PersistenceService

        storage = PersistenceService(db_path)
        client = TestClient(create_app(storage),
                            raise_server_exceptions=False)

        # ---- Stage 2: enrich (read-only live fetching) ---------------------
        records = load_seed_records()
        enriched = stage2_enrich(records, report)

        # ---- Stage 3: ingest into scratch DB -------------------------------
        run_id = stage3_ingest(client, enriched, report, "e2e-workflow")

        # Station identity keys (domain:...).
        keys = []
        for rec in enriched:
            from enrichment.dedupe import identity_key
            kind, value = identity_key(rec)
            keys.append(f"{kind}:{value}")

        # ---- Stage 4: stations ----------------------------------------------
        stage4_stations(client, keys, report)

        # ---- Stage 5: intelligence ------------------------------------------
        # WFMU is the seed known to yield named contacts; assert on it with a
        # structural (not count-specific) expectation. All keys still checked.
        for key in keys:
            is_wfmu = "wfmu" in (key or "").lower()
            stage5_intelligence(client, key, wfmu=is_wfmu, report=report)

        # ---- Stage 6: contacts ----------------------------------------------
        for key in keys:
            stage6_contacts(client, key, report)

        # ---- Runs + idempotency + 404s ---------------------------------------
        if run_id:
            stage_runs(client, run_id, report)
        stage_idempotent(client, enriched, report)
        stage_404s(client, report)

        # ---- Live dry-run (opt-in; not used here) ----------------------------
        if args.live_base_url:
            print("NOTE: --live-base-url dry-run requires --confirm-live and "
                  "has not been executed.")

        print("\n=== END-TO-END WORKFLOW REPORT ===")
        print(report.summary())
        storage.close()
        failed = sum(1 for _, _, ok in report.checks if not ok)
        if failed:
            print("\nRESULT: FAILURES PRESENT")
            return 1
        print("\nRESULT: ALL CHECKS PASSED")
        return 0
    finally:
        if not args.keep_db:
            try:
                tmp.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
