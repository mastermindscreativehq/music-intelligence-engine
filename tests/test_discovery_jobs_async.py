"""Focused tests: automated discovery jobs are now submitted asynchronously.

POST /api/v1/discovery/jobs validates the envelope, persists a ``queued``
ledger row, and answers 202 immediately with an opaque ``run_id``. A single
background worker per process then runs the EXACT same radio/station pipeline
off the request path (DJ / SerpAPI / enrichment / ingest are untouched),
while GET /api/v1/discovery/jobs/{run_id} polls the durable ledger:

    queued -> running -> completed | completed_with_failures |
                            not_configured | failed

Verification strategy (mirrors tests/test_station_research.py): injected
fake fetchers + local HTML fixtures — the ONLY real-HTTP risk would be a live
provider call, so every test either injects ``_Web`` or clears the provider
env. httpx/FastAPI are NOT required: ``backend.routes.dispatch`` is driven
directly — the framework-free single source of truth shared by all servers.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from backend.routes import dispatch

from database.service import PersistenceService

from discovery.async_discovery import DiscoveryJobScheduler
from discovery.djs.serpapi_provider import SERPAPI_API_KEY_ENV

from tests.test_station_research import _Web as _StationWeb
from tests.test_station_research import _station_pages

_KEY = "TEST-STATION-KEY"
_TOKEN = "MIE-TEST-TOKEN"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _radio_payload() -> dict:
    return {"organization_type": "radio",
            "query": "community radio", "limit": 25}


def _post(repo, scheduler, payload, *, fetcher=None, headers=_AUTH):
    body = json.dumps(payload).encode("utf-8")
    return dispatch(
        repo, "POST", "/api/v1/discovery/jobs", {}, body,
        discover_fetcher=fetcher, headers=headers,
        discovery_scheduler=scheduler)


def _get(repo, run_id, *, headers=_AUTH):
    return dispatch(repo, "GET", f"/api/v1/discovery/jobs/{run_id}", {},
                    headers=headers)


class _HoldingWeb(_StationWeb):
    """Blocks every NON-SerpAPI fetch until ``gate`` is set.

    Lets a test hold a job mid-flight (status ``running``) and prove the
    status GET answers while the worker is still working — the exact reason
    the worker runs on its own storage connection (repository.clone()).
    """

    def __init__(self, gate: threading.Event, **kwargs):
        super().__init__(**kwargs)
        self._gate = gate

    def fetch(self, url, **kwargs):
        if "serpapi.com/search" not in url and "/search?" not in url:
            self._gate.wait(timeout=30)
        return super().fetch(url, **kwargs)


class AsyncDiscoveryContractTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = PersistenceService(os.path.join(self._tmp.name, "mie.db"))
        self._env = mock.patch.dict(
            os.environ,
            {SERPAPI_API_KEY_ENV: _KEY, "MIE_AUTOMATION_TOKEN": _TOKEN},
            clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.repo.close()
        self._tmp.cleanup()

    def _poll_terminal(self, run_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            status, body = _get(self.repo, run_id)
            self.assertEqual(status, 200)
            run = body["data"]
            seen.append(run["status"])
            if run["status"] not in ("queued", "running"):
                # Let the worker's own storage close() land before tearDown
                # removes the temp dir (Windows cannot delete a file whose
                # handle is still open).
                time.sleep(0.05)
                return run
            time.sleep(0.02)
        self.fail(f"job {run_id!r} never reached a terminal state; seen={seen}")

    def test_submit_answers_202_then_worker_completes_with_metrics(self):
        gate = threading.Event()
        scheduler = DiscoveryJobScheduler(
            self.repo, fetcher=_HoldingWeb(gate=gate,
                                           pages=_station_pages()))

        status, body = _post(self.repo, scheduler, _radio_payload())
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertTrue(data["run_id"].startswith("job_"))
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["organization_type"], "radio")
        run_id = data["run_id"]

        queued = self.repo.get_discovery_job(run_id)
        self.assertIsNotNone(queued)
        self.assertIn(queued["status"], ("queued", "running"))

        # The status GET answers promptly while the job is held mid-flight:
        # a GET must never block on the background worker.
        started = time.monotonic()
        status, body = _get(self.repo, run_id)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(status, 200)
        self.assertIn(body["data"]["status"], ("queued", "running"))

        # While the worker is blocked inside the pipeline, the durable
        # "running" state becomes visible before the terminal row.
        deadline = time.monotonic() + 10
        seen: list[str] = []
        while time.monotonic() < deadline and "running" not in seen:
            _, body = _get(self.repo, run_id)
            seen.append(body["data"]["status"])
            time.sleep(0.01)
        self.assertIn("running", seen)

        gate.set()
        run = self._poll_terminal(run_id)
        self.assertIn(run["status"],
                      ("completed", "completed_with_failures"))
        self.assertEqual(run["provider"], "serpapi_google")
        self.assertGreaterEqual(run["records_ingested"], 1)
        for key in ("queries_run", "candidates_found", "records_ingested",
                    "failures", "started_at", "completed_at"):
            self.assertIn(key, run)
        station = self.repo.get_station("domain:kqxr.example")
        self.assertIsNotNone(station)

    def test_get_unknown_run_id_is_404(self):
        status, body = _get(self.repo, "job_" + "a" * 24)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "run_not_found")

    def test_invalid_organization_type_is_400(self):
        scheduler = DiscoveryJobScheduler(self.repo, fetcher=_StationWeb())
        status, body = _post(
            self.repo, scheduler,
            {"organization_type": "venue", "query": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_unconfigured_provider_terminates_not_configured(self):
        # Keep the provider env empty for the WHOLE test (also while the
        # background worker runs), so the run must end honestly as
        # not_configured instead of attempting a live provider call.
        self._env.stop()
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()
        scheduler = DiscoveryJobScheduler(
            self.repo, fetcher=_StationWeb(pages=_station_pages()))
        status, body = _post(self.repo, scheduler, _radio_payload(),
                             headers={})
        self.assertEqual(status, 202)
        run = self._poll_terminal(body["data"]["run_id"])
        self.assertEqual(run["status"], "not_configured")
        self.assertIsNotNone(run["error"])

    def test_one_ledger_row_lives_through_status_transitions(self):
        # queued -> running -> completed must rewrite ONE durable row, never
        # append duplicates (the GET/poll contract assumes exactly one).
        run_id = "job_" + "0" * 24
        for status in ("queued", "running", "completed"):
            self.repo.record_discovery_job({
                "run_id": run_id, "organization_type": "radio",
                "config": _radio_payload(), "provider": None,
                "queries_run": 0, "candidates_found": 0,
                "records_ingested": 0, "duplicates": 0, "failures": 0,
                "status": status, "error": None,
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:00:00Z"
                if status == "completed" else None})
        row = self.repo.get_discovery_job(run_id)
        self.assertEqual(row["status"], "completed")
        db = sqlite3.connect(os.path.join(self._tmp.name, "mie.db"))
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM discovery_jobs WHERE run_id = ?",
                (run_id,)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)

    def test_dj_submission_uses_the_same_async_contract(self):
        # The DJ runner itself is unchanged (existing Phase-4 tests cover it);
        # this only proves organization_type=dj enqueues and drains through
        # the same 202 + durable-run contract with an injected fetcher so no
        # real HTTP is ever attempted (zero candidate pages -> terminal row).
        scheduler = DiscoveryJobScheduler(
            self.repo, fetcher=_StationWeb(pages={}))
        status, body = _post(self.repo, scheduler, {
            "organization_type": "dj", "query": "independent DJ",
            "genre": "Afrobeats", "country": "United States", "limit": 25})
        self.assertEqual(status, 202)
        self.assertEqual(body["data"]["status"], "queued")
        run = self._poll_terminal(body["data"]["run_id"])
        self.assertIn(
            run["status"],
            ("completed", "completed_with_failures",
             "not_configured", "failed"))

    def test_legacy_synchronous_contract_when_no_scheduler_wired(self):
        # Direct callers / tests (scheduler=None) keep the historical
        # blocking contract: POST answers 200 with the full run report.
        status, body = _post(
            self.repo, None, _radio_payload(),
            fetcher=_StationWeb(pages=_station_pages()))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        report = body["data"]
        self.assertIn(report["status"],
                      ("completed", "completed_with_failures"))
        self.assertGreaterEqual(report["records_ingested"], 1)
        self.assertIsNotNone(self.repo.get_station("domain:kqxr.example"))


if __name__ == "__main__":
    unittest.main()