"""Asynchronous execution for automation discovery jobs (Phase 11).

POST /api/v1/discovery/jobs now validates the envelope, persists a
``queued`` ledger row, and returns 202 immediately with the opaque
``run_id``. A single daemon worker thread per process (started lazily on the
first submission) drains the queue and runs the EXISTING organization-type
runner — discovery, SerpAPI, enrichment, and ingest are untouched; only the
submission contract changes.

Ledger lifecycle (``discovery_jobs``, persisted via ``record_discovery_job``):

    queued                     written by the submission endpoint
    running                    worker start (before the long pipeline)
    completed | completed_with_failures | not_configured | failed
                               terminal rows written by ``execute_discovery_job``

The worker executes on its OWN storage connection (``repository.clone()``) so
an in-flight job — which can take minutes of SerpAPI + enrichment work and a
long ingest transaction — never holds the shared request-thread connection.
Polling ``GET /api/v1/discovery/jobs/{run_id}`` therefore never blocks on the
job. Jobs are executed serially by the single worker, so the shared
in-process fetcher and the ``EnrichmentEngine`` live-mode switch are never
raced.

Restart semantics: ledger rows are durable. If the process stops while a job
is queued/running, the persisted state survives and the status endpoint keeps
surfacing it honestly — nothing is fabricated, dropped, or silently re-queued.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

logger = logging.getLogger("mie.discovery.async")

# Statuses the ledger is expected to carry (requirement: queued / running /
# completed / completed_with_failures / failed; not_configured preserved as an
# existing honest terminal state).
QUEUED = "queued"
RUNNING = "running"


class DiscoveryJobScheduler:
    """FIFO background executor for automation discovery jobs.

    ``storage`` must offer ``clone()`` returning an independent storage bound
    to the same database (own connection); ``fetcher`` is the shared injected
    fetcher passed to the discovery pipeline (None = live provider).
    """

    def __init__(self, storage: Any, *, fetcher: Any = None) -> None:
        self._storage = storage
        self._fetcher = fetcher
        self._queue: "queue.SimpleQueue[tuple[str, str, dict, str]]" = (
            queue.SimpleQueue())
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def _start(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(
                target=self._run, name="mie-discovery-worker", daemon=True)
            thread.start()
            self._thread = thread

    def submit(self, run_id: str, org_type: str, config: dict,
               started_at: str) -> None:
        """Persist the queued row and hand the job to the worker.

        Runs synchronously but acts only as a tiny ledger write + queue push:
        no discovery happens inside the submission request.
        """
        self._storage.record_discovery_job({
            "run_id": run_id,
            "organization_type": org_type,
            "config": dict(config),
            "provider": None,
            "status": QUEUED,
            "queries_run": 0,
            "candidates_found": 0,
            "records_ingested": 0,
            "duplicates": 0,
            "failures": 0,
            "error": None,
            "started_at": started_at,
            "completed_at": None,
        })
        self._start()
        self._queue.put((run_id, org_type, dict(config), started_at))

    def _run(self) -> None:  # daemon worker; never raises out of the loop
        from discovery.jobs import execute_discovery_job
        while True:
            run_id, org_type, config, started_at = self._queue.get()
            try:
                repository = self._storage.clone()
            except Exception:
                logger.exception(
                    "discovery job %s could not open its own storage; the "
                    "durable row stays %s and is answered by status GETs",
                    run_id, QUEUED)
                continue
            try:
                execute_discovery_job(
                    repository, run_id, org_type, config,
                    fetcher=self._fetcher, started_at=started_at,
                    record_running=True)
            except Exception:
                # execute_discovery_job persists the terminal failed /
                # not_configured row before re-raising; the worker must keep
                # draining the queue regardless.
                logger.exception(
                    "discovery job %s raised after its terminal state was "
                    "recorded; continuing with the queue", run_id)
            finally:
                # Always release the job's own connection so a long-lived
                # worker never leaks one handle per job (SQLite holds a file
                # lock on Windows; Postgres leaks a live connection).
                try:
                    repository.close()
                except Exception:
                    logger.exception(
                        "discovery job %s storage close failed; its durable "
                        "state stays persisted and answerable via GET",
                        run_id)