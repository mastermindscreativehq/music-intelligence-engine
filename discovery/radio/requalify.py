"""One-time re-qualification of ALREADY-STORED station records.

Re-evaluates rows that production already stored against the CURRENT
qualification rules, using each record's own stored website as the research
target, and emits a DRY-RUN report. This module is read-only by
construction: it contains no apply/ingest/update path at all.

Reuse, not re-implementation
----------------------------
Every verdict comes from the one qualification engine
(:mod:`discovery.radio.qualify`) and the same crawl/enrichment helpers the
live pipeline uses (:mod:`crawler`, :mod:`enrichment.geography`,
:mod:`enrichment.stations`). This is an orchestration shell, never a second
set of qualification rules. The post-fetch call is the same one
``discovery.radio.pipeline`` makes, with the same arguments, so:

* article-path precedence is preserved (per-item URL -> ``REJECTED``);
* first-person self-identification is required (a page that only *mentions*
  a station lands in ``needs_review``);
* sub-pages cannot promote a candidate (only ``pages[0]`` is ``texts``;
  the rest are ``secondary_texts``);
* evidence-based geography is preserved.

Evidence discipline
-------------------
* The research target is the record's own ``website``/``domain``. The
  original discovery query is never used, and no search snippet is passed
  (``snippet=""``) — the site's own pages are the only evidence.
* Geography comes from ``extract_location`` over the record's own fetched
  pages/title/domain. No discovery-request scope is carried in, and the
  stored country/state/city is reported for comparison but never fed back
  in as an input.
* Unknown stays ``None``. Nothing is invented to fill a bucket.

Safety
------
* :func:`requalify_record` takes an :class:`ExistingStation` and a fetcher.
  It is never handed a repository, a connection, or a service object, so it
  is structurally incapable of writing.
* The only persistence path in the radio pipeline is
  ``stations.service.ingest_station_discovery`` (called from
  :func:`discovery.radio.jobs.run_radio_discovery_job`). This module does
  not import it; a test enforces that.
* :func:`load_existing_stations` is the only function that touches a
  database. It requires ``dry_run=True``, opens the connection with
  ``read_only=True``, issues SELECTs only, and rolls back.

CLI::

    python -m discovery.radio.requalify --dry-run --offline
    python -m discovery.radio.requalify --dry-run --report-out report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from crawler.http import DEFAULT_USER_AGENT, StdlibHttpFetcher, utc_now_iso
from crawler.page_finder import select_priority_pages
from crawler.pages import parse_html
from crawler.urls import InvalidUrlError, normalize_url

from discovery.radio.pipeline import EngineConfig, clean_title
from discovery.radio.qualify import (
    NEEDS_REVIEW,
    QUALIFIED,
    REJECTED,
    classify_station_candidate,
    classify_station_site,
)
from enrichment.geography import extract_location
from enrichment.stations import classify_station

logger = logging.getLogger("mie.discovery.radio.requalify")

# ---------------------------------------------------------- network bounds
#
# The shared fetcher (crawler.http.StdlibHttpFetcher) already accepts a
# per-call ``timeout=``; we use it. That is NOT sufficient on its own:
# ``resp.read()`` loops internally, and the socket timeout only bounds each
# individual ``recv()``. A server that trickles one byte at a time can keep
# that loop alive indefinitely, and ``robots.allows()`` runs before the
# request timeout is even applied. We therefore ALSO cap each fetch with a
# wall-clock deadline enforced here, and abandon the worker thread when it
# expires. crawler/http.py is shared with live discovery and is left alone.
DEFAULT_FETCH_TIMEOUT_SECONDS = 15.0
DEFAULT_RECORD_BUDGET_SECONDS = 120.0
# How often a still-running fetch reports that it is alive, so an operator
# watching a live run can tell "slow site" from "wedged process".
_HEARTBEAT_EVERY = 10.0

# --- transient-failure policy ---------------------------------------------
# A DNS/connection blip or a 429/503 is a property of the network at that
# moment, not of the station. Retrying those is what stops a 4-second
# outage from being recorded as 455 permanently broken domains. Everything
# else -- robots.txt policy, a 404, a deterministic article path -- is a
# definitive answer and is never retried.
RETRYABLE_ERROR_KINDS = frozenset({"dns_error", "connection_error"})
RETRYABLE_HTTP_STATUS = frozenset({429, 503})
# "2 retry attempts total" == the first call plus 2 retries == 3 calls.
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 3.0

# --- network-outage detection ---------------------------------------------
# If this fraction of the recent window failed for TRANSIENT NETWORK reasons,
# the run is measuring the network, not the stations, and its report must not
# be trusted. The failure set is exactly the retry allowlist above
# (dns_error, connection_error, HTTP 429/503) -- slow sites and robots.txt
# blocks are NOT outages and must not be able to abort a healthy run.
DEFAULT_OUTAGE_WINDOW = 20
DEFAULT_OUTAGE_FAILURE_RATIO = 0.5
OUTAGE_ABORT_MESSAGE = (
    "NETWORK OUTAGE DETECTED - aborting requalification before producing a "
    "misleading report.")


def is_transient_network_failure(kind: str | None, status: int | None) -> bool:
    """True only for failures a retry could plausibly fix.

    Deliberately excludes robots.txt decisions and ordinary 4xx statuses:
    those are answers, not interruptions.
    """
    if kind in RETRYABLE_ERROR_KINDS:
        return True
    return status in RETRYABLE_HTTP_STATUS


def classify_transient_outcome(outcome: RequalificationOutcome) -> bool:
    """Did this record fail because the NETWORK misbehaved?

    This is deliberately the SAME predicate the retry policy uses
    (:func:`is_transient_network_failure`): dns_error, connection_error,
    HTTP 429 and HTTP 503. Nothing else counts.

    Two categories are explicitly excluded, and the distinction matters:

    * ``fetch_timeout`` -- one slow site, not a broken network. A station
      serving a large page over a slow link can blow the per-site cap while
      every other host is perfectly healthy. Counting those made a run of
      slow sites abort as a "network outage", which is a false accusation
      against the network and needlessly discards hundreds of good results.
    * ``robots_disallowed`` -- a policy decision, not an interruption. It is
      definitive, never retried, and will never resolve by retrying.

    Deterministic rejections are excluded too: rejecting 20 article paths is
      a healthy run, not an outage.
    """
    if outcome.proposed_verdict == REJECTED:
        return False
    return is_transient_network_failure(
        outcome.network_error_kind, outcome.network_error_status)


class OutageDetector:
    """Rolling transient-network-failure detector over the last N records.

    Deliberately simple and injectable so the threshold is testable without
    any network. ``record()`` returns True once the window is full and the
    transient-failure ratio exceeds the threshold.

    The counted failures come solely from
    :func:`classify_transient_outcome`, i.e. the same allowlist the retry
    policy uses: dns_error, connection_error, HTTP 429/503. A run full of
    slow sites or robots.txt refusals is a legitimate run and must complete.
    """

    def __init__(self, window: int = DEFAULT_OUTAGE_WINDOW,
                 threshold: float = DEFAULT_OUTAGE_FAILURE_RATIO,
                 records_total: int = 0) -> None:
        self.window = max(1, int(window))
        self.threshold = float(threshold)
        self.records_total = int(records_total)
        self._kinds: list[str] = []
        self.aborted = False
        self.aborted_at: int | None = None

    @property
    def samples(self) -> int:
        return len(self._kinds)

    @property
    def failure_rate(self) -> float:
        if not self._kinds:
            return 0.0
        failures = sum(1 for k in self._kinds if k == "fail")
        return failures / len(self._kinds)

    def record(self, outcome: RequalificationOutcome) -> bool:
        """Feed one outcome; True means the run should abort now."""
        self._kinds.append(
            "fail" if classify_transient_outcome(outcome) else "ok")
        if len(self._kinds) > self.window:
            del self._kinds[0]
        # Only judge once the window is full, so a handful of early failures
        # can never trigger a false abort.
        if len(self._kinds) >= self.window:
            if self.failure_rate > self.threshold:
                self.aborted = True
                if self.aborted_at is None:
                    self.aborted_at = self.samples
                return True
        return False
# Transport-level failures we translate into a deterministic result rather
# than letting them escape or turn into a REJECTED verdict.
TRANSPORT_ERRORS = (
    socket.timeout, TimeoutError, ConnectionError, ssl.SSLError,
    ssl.CertificateError, OSError,
)

# How a proposed verdict was reached. Used to separate "obviously garbage,
# decided with no network" from "needs a live fetch to be decided".
DETERMINISTIC = "deterministic"        # decided offline, no fetch needed
NETWORK_VERIFIED = "network_verified"  # decided from live page evidence
NETWORK_REQUIRED = "network_required"  # cannot be decided without a fetch

BUCKETS = (QUALIFIED, NEEDS_REVIEW, REJECTED)


class ReadOnlyViolation(RuntimeError):
    """Raised when a caller tries to use this tool in a writing mode.

    The tool has no apply path. This exists so an accidental future call
    fails loudly instead of silently becoming an ingest.
    """


# --------------------------------------------------------------------- models


@dataclass(frozen=True)
class ExistingStation:
    """A station record as ALREADY STORED, read verbatim from the database.

    A plain frozen value object: it carries no connection and no service, so
    handing it to the runner cannot hand over a way to write.
    """

    station_id: str
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    current_status: str | None = None
    current_verdict: str | None = None
    current_kind: str | None = None
    current_reason: str | None = None
    current_homepage_title: str | None = None
    current_country: str | None = None
    current_state_or_region: str | None = None
    current_city: str | None = None
    current_station_type: str | None = None
    first_stored_at: str | None = None
    last_stored_at: str | None = None

    @property
    def current_label(self) -> str:
        return self.current_verdict or "none"


@dataclass(frozen=True)
class RequalificationOutcome:
    """The dry-run proposal for one existing record. Never persisted."""

    station_id: str
    name: str | None
    domain: str | None
    website: str | None
    current_status: str | None
    current_verdict: str | None
    proposed_verdict: str
    proposed_kind: str
    reason: str
    evidence: tuple[str, ...] = ()
    determinism: str = DETERMINISTIC
    pre_fetch_verdict: str | None = None
    pages_fetched: int = 0
    proposed_country: str | None = None
    proposed_state_or_region: str | None = None
    proposed_city: str | None = None
    proposed_station_type: str | None = None
    errors: tuple[str, ...] = ()
    evaluated_at: str = ""
    # The UNDERLYING transport failure behind ``proposed_kind``. The
    # requalify-level kind is a coarse label ("fetch_failed") that merges
    # robots.txt decisions, HTTP statuses, TLS problems and DNS blips; the
    # outage guard needs the precise reason to tell a broken network from a
    # site that simply does not want to be crawled.
    network_error_kind: str | None = None
    network_error_status: int | None = None

    @property
    def is_transient_network_failure(self) -> bool:
        """Single source of truth for both the retry and outage policies."""
        return is_transient_network_failure(
            self.network_error_kind, self.network_error_status)

    @property
    def is_deterministic_garbage(self) -> bool:
        """Obvious garbage: a non-station decided with no network access."""
        return (self.proposed_verdict == REJECTED
                and self.determinism == DETERMINISTIC)

    @property
    def current_label(self) -> str:
        return self.current_verdict or "none"

    @property
    def needs_network_verification(self) -> bool:
        return self.determinism == NETWORK_REQUIRED

    def to_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "name": self.name,
            "domain": self.domain,
            "website": self.website,
            "current_status": self.current_status,
            "current_verdict": self.current_verdict,
            "proposed_verdict": self.proposed_verdict,
            "proposed_kind": self.proposed_kind,
            "reason": self.reason,
            "network_error_kind": self.network_error_kind,
            "network_error_status": self.network_error_status,
            "is_transient_network_failure": self.is_transient_network_failure,
            "evidence": list(self.evidence),
            "determinism": self.determinism,
            "pre_fetch_verdict": self.pre_fetch_verdict,
            "pages_fetched": self.pages_fetched,
            "proposed_location": {
                "country": self.proposed_country,
                "state_or_region": self.proposed_state_or_region,
                "city": self.proposed_city,
            },
            "proposed_station_type": self.proposed_station_type,
            "errors": list(self.errors),
            "evaluated_at": self.evaluated_at,
        }


# ------------------------------------------------------------------- helpers


def resolve_target(station: ExistingStation) -> tuple[str | None, str]:
    """The record's OWN website as the research target.

    Prefers the stored ``website``; falls back to ``https://<domain>``. The
    original discovery query is never consulted.
    """
    if station.website:
        try:
            return normalize_url(station.website), ""
        except (InvalidUrlError, ValueError):
            # Keep the stored value verbatim: the gates still inspect it, and
            # discarding it would hide a real malformed-URL problem.
            return station.website.strip(), ""
    if station.domain:
        return f"https://{station.domain.strip()}", ""
    return None, "record has neither website nor domain to re-check"


def _empty_outcome(
    station: ExistingStation,
    *,
    verdict: str,
    kind: str,
    reason: str,
    target: str | None = None,
    determinism: str = DETERMINISTIC,
    evidence: Sequence[str] = (),
    pre_fetch_verdict: str | None = None,
    errors: Sequence[str] = (),
    network_error_kind: str | None = None,
    network_error_status: int | None = None,
) -> RequalificationOutcome:
    return RequalificationOutcome(
        station_id=station.station_id,
        name=station.name,
        domain=station.domain,
        website=target if target is not None else station.website,
        current_status=station.current_status,
        current_verdict=station.current_verdict,
        proposed_verdict=verdict,
        proposed_kind=kind,
        reason=reason,
        evidence=tuple(evidence),
        determinism=determinism,
        pre_fetch_verdict=pre_fetch_verdict,
        errors=tuple(errors),
        evaluated_at=utc_now_iso(),
        network_error_kind=network_error_kind,
        network_error_status=network_error_status,
    )


# ---------------------------------------------------------------- the runner


@dataclass(frozen=True)
class BoundedFetch:
    """Outcome of one guarded fetch.

    ``timed_out`` means our wall-clock cap expired and the worker thread was
    abandoned. ``error`` is a transport failure that escaped the fetcher.
    """

    result: Any | None = None
    timed_out: bool = False
    error: BaseException | None = None
    waited: float = 0.0


def fetch_within(
    fetcher: Any,
    url: str,
    *,
    wall_timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    socket_timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    heartbeat=None,
) -> BoundedFetch:
    """Fetch ``url`` under a hard wall-clock cap.

    Runs the call on a daemon thread so a hung socket cannot block the run:
    when ``wall_timeout`` expires we stop waiting and report a timeout. The
    abandoned thread is left to die on its own (it holds no database handle
    and writes nothing), and being a daemon it cannot delay exit.

    The fetcher's own ``timeout=`` kwarg is also supplied so the shared
    client uses the same bound where it can. A caller-injected fetcher that
    does not accept ``timeout`` still works.
    """
    box: dict[str, Any] = {}
    state: dict[str, float] = {"last": time.monotonic()}
    supports_timeout = True

    def _call() -> None:
        nonlocal supports_timeout
        try:
            box["result"] = fetcher.fetch(url, timeout=socket_timeout)
        except TypeError as exc:
            if "timeout" not in str(exc):
                box["error"] = exc
                return
            # Fetcher does not accept the kwarg; fall back to a plain call.
            supports_timeout = False
            try:
                box["result"] = fetcher.fetch(url)
            except BaseException as exc:  # captured, re-raised by the caller
                box["error"] = exc
        except BaseException as exc:  # noqa: BLE001 - captured deliberately
            box["error"] = exc

    started = time.monotonic()
    thread = threading.Thread(target=_call, daemon=True,
                              name="requalify-fetch")
    thread.start()
    deadline = started + max(0.0, float(wall_timeout))
    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return BoundedFetch(timed_out=True, waited=time.monotonic() - started)
        now = time.monotonic()
        if heartbeat is not None and now - state["last"] >= _HEARTBEAT_EVERY:
            state["last"] = now
            try:
                heartbeat(url, time.monotonic() - started, remaining)
            except Exception:  # never let reporting break the run
                pass
        thread.join(min(0.25, remaining))
    if "error" in box:
        return BoundedFetch(error=box["error"],
                            waited=time.monotonic() - started)
    return BoundedFetch(result=box.get("result"),
                        waited=time.monotonic() - started)


def fetch_with_retries(
    fetcher: Any,
    url: str,
    *,
    wall_timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    socket_timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    budget_left=None,
    heartbeat=None,
    on_retry=None,
) -> BoundedFetch:
    """Fetch with bounded retries for transient network failures only.

    ``retry_attempts`` counts retries *after* the first call, so the default
    of 2 means at most 3 calls. Only dns_error, connection_error and HTTP
    429/503 are retried: a robots.txt decision, a 404 or a page-level timeout
    is a definitive answer and is returned immediately.

    ``budget_left`` is an optional callable returning the record's remaining
    seconds; a retry is skipped once the record budget is spent, so retries
    can never extend a record past its cap.
    """
    attempts = max(0, int(retry_attempts)) + 1
    last = BoundedFetch(result=None)
    for attempt in range(attempts):
        remaining = budget_left() if budget_left is not None else wall_timeout
        if attempt > 0 and remaining <= 0:
            # Out of record budget: report what we have rather than overrun.
            return last
        got = fetch_within(
            fetcher, url,
            wall_timeout=min(wall_timeout, remaining) if remaining > 0
            else wall_timeout,
            socket_timeout=min(socket_timeout, remaining) if remaining > 0
            else socket_timeout,
            heartbeat=heartbeat)
        last = got
        if got.timed_out or got.error is not None or got.result is None:
            return got
        result = got.result
        if result.ok:
            return got
        if not is_transient_network_failure(result.error_kind,
                                            result.status):
            return got
        if attempt == attempts - 1:
            return got
        if on_retry is not None:
            on_retry(attempt + 1, result)
        if retry_backoff > 0:
            # Sleep in slices so a Ctrl-C stays responsive and the record
            # budget is not blown while backing off.
            deadline = time.monotonic() + float(retry_backoff)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                if budget_left is not None and budget_left() <= 0:
                    return last
                time.sleep(min(0.25, left))
    return last


def requalify_record(
    station: ExistingStation,
    *,
    fetcher: Any | None = None,
    config: EngineConfig | None = None,
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    record_budget: float = DEFAULT_RECORD_BUDGET_SECONDS,
    heartbeat=None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> RequalificationOutcome:
    """Re-evaluate ONE existing record against the current rules.

    No repository, no connection, no service object: this function cannot
    write to anything. Pass ``fetcher=None`` for an offline pass that only
    reports what can be decided without a fetch.

    Every network call is bounded by ``fetch_timeout`` (hard wall clock) and
    the record as a whole by ``record_budget``. A slow, dead or hostile site
    therefore produces a deterministic ``needs_review`` result and the run
    moves on -- it is never reported as ``rejected``, because an unreachable
    site is not evidence of a non-station.

    Transient network failures (dns_error, connection_error, HTTP 429/503)
    are retried ``retry_attempts`` times with ``retry_backoff`` seconds
    between attempts, because those describe the network rather than the
    station. Definitive answers are not retried.
    """
    config = config or EngineConfig()
    station_started = time.monotonic()
    n_fetch = {"n": 0}

    def _beat(url: str, waited: float, remaining: float) -> None:
        if heartbeat is None:
            return
        n_fetch["n"] += 1
        heartbeat(station, url=url, waited=waited, remaining=remaining,
                  fetch_index=n_fetch["n"],
                  elapsed=time.monotonic() - station_started)

    target, target_error = resolve_target(station)
    if not target:
        return _empty_outcome(
            station, verdict=NEEDS_REVIEW, kind="unusable_target",
            reason=target_error)

    # --- pre-fetch gate: the CURRENT candidate rules on the stored URL -----
    # title: the site's OWN stored title (not a discovery-query title).
    # snippet: deliberately empty -- search snippets are not evidence here.
    pre = classify_station_candidate(
        url=target, title=station.current_homepage_title or "")
    if pre.verdict == REJECTED:
        # Determined with no network at all: a per-item/article path, a
        # denied host or a platform page can never be a station's own site.
        return _empty_outcome(
            station, verdict=REJECTED, kind=pre.kind, reason=pre.reason,
            target=target, determinism=DETERMINISTIC, evidence=pre.evidence,
            pre_fetch_verdict=pre.verdict)

    if fetcher is None:
        # The post-fetch gate decides identity, and it needs page text that
        # was never persisted. Reporting QUALIFIED off the pre-fetch gate
        # alone would over-claim, so the record stays quarantined and is
        # flagged as needing a live fetch.
        return _empty_outcome(
            station, verdict=NEEDS_REVIEW,
            kind="unverified_pending_live_fetch",
            reason=("pre-fetch gate passed (" + (pre.reason or "no detail")
                    + "); final verdict needs a live fetch of the station's "
                      "own site, which this pass did not make"),
            target=target, determinism=NETWORK_REQUIRED, evidence=pre.evidence,
            pre_fetch_verdict=pre.verdict)

    errors: list[str] = []
    record_started = time.monotonic()

    def _budget_left() -> float:
        return max(0.0, float(record_budget) - (time.monotonic()
                                                 - record_started))

    # --- fetch the record's own site, with transient-failure retries ------
    def _on_retry(attempt: int, result: Any) -> None:
        errors.append(
            f"attempt {attempt} failed transiently "
            f"({result.error_kind or 'unknown'}"
            f"{'/' + str(result.status) if result.status else ''}); retrying")
        _beat(target, 0.0, _budget_left())

    home_fetch = fetch_with_retries(
        fetcher, target,
        wall_timeout=min(float(fetch_timeout), _budget_left()),
        socket_timeout=min(float(fetch_timeout), _budget_left()),
        retry_attempts=retry_attempts,
        retry_backoff=retry_backoff,
        budget_left=_budget_left,
        heartbeat=_beat,
        on_retry=_on_retry)
    if home_fetch.timed_out:
        return _empty_outcome(
            station, verdict=NEEDS_REVIEW, kind="fetch_timeout",
            reason=(f"gave up after {home_fetch.waited:.1f}s waiting for "
                    f"{target} (per-site limit reached); the site may be slow "
                    "or unreachable, which is not evidence it is not a "
                    "station"),
            target=target, determinism=NETWORK_REQUIRED, evidence=pre.evidence,
            pre_fetch_verdict=pre.verdict,
            errors=[f"fetch_timeout url={target}"],
            # Explicitly NOT a transient network failure: a per-site wall
            # clock expiry says this one site was slow, not that the network
            # is down. Recorded as its own kind so the outage guard can
            # never mistake it for dns_error/connection_error/429/503.
            network_error_kind="per_site_timeout")
    if home_fetch.error is not None:
        error = home_fetch.error
        if not isinstance(error, Exception):
            # SystemExit / KeyboardInterrupt stay fatal: an operator must be
            # able to stop the run.
            raise error
        return _empty_outcome(
            station, verdict=NEEDS_REVIEW, kind="fetch_error",
            reason=(f"transport failure fetching the stored site: "
                    f"{type(error).__name__}: {error}"),
            target=target, determinism=NETWORK_REQUIRED, evidence=pre.evidence,
            pre_fetch_verdict=pre.verdict,
            errors=[f"{type(error).__name__}: {error}"],
            # A raised exception is not one of the four transient kinds
            # unless it says so; keep it out of the outage signal.
            network_error_kind="transport_exception")
    home_result = home_fetch.result
    if home_result is None:
        return _empty_outcome(
            station, verdict=NEEDS_REVIEW, kind="fetch_error",
            reason="fetcher returned no result for the stored site",
            target=target, determinism=NETWORK_REQUIRED, evidence=pre.evidence,
            pre_fetch_verdict=pre.verdict, errors=["no fetch result"],
            network_error_kind="no_result")
    if not home_result.ok:
        # A site we cannot reach is not proof of a non-station: quarantine
        # and say so rather than guessing. The precise error_kind/status is
        # preserved so the outage guard applies the same allowlist as retry.
        return _empty_outcome(
            station, verdict=NEEDS_REVIEW, kind="fetch_failed",
            reason=("could not fetch the stored site: "
                    + (home_result.error_kind or "unknown_error")),
            target=target, determinism=NETWORK_REQUIRED, evidence=pre.evidence,
            pre_fetch_verdict=pre.verdict,
            errors=[home_result.error_message or home_result.error_kind
                    or "fetch failed"],
            network_error_kind=home_result.error_kind,
            network_error_status=home_result.status)

    home_url = home_result.final_url or target
    home_page = parse_html(home_url, home_result.body or "")
    homepage_title = clean_title(home_page.title)
    pages = [home_page]

    # --- early hard gate, identical to the live pipeline -------------------
    early = classify_station_site(
        website_url=target,
        homepage_title=homepage_title,
        texts=(home_page.text or "",))
    if early.verdict == REJECTED:
        return _empty_outcome(
            station, verdict=REJECTED, kind=early.kind, reason=early.reason,
            target=target, determinism=NETWORK_VERIFIED,
            evidence=early.evidence, pre_fetch_verdict=pre.verdict,
            errors=errors)

    # --- focused sub-page discovery (sub-pages may never promote) ---------
    # Each sub-page is bounded like the homepage and the record as a whole is
    # bounded by ``record_budget``: a site that stalls must not be able to
    # spend the whole run. Sub-pages can never promote a record, so losing
    # them only removes corroborating evidence -- it never changes a
    # qualified/rejected decision in the optimistic direction.
    budget = max(0, config.max_pages_per_site - 1)
    for url in select_priority_pages(home_url, home_page, budget):
        remaining = _budget_left()
        if remaining <= 0:
            errors.append(
                f"record budget of {record_budget:.0f}s exhausted before "
                f"subpage {url}")
            break
        page_fetch = fetch_with_retries(
            fetcher, url,
            wall_timeout=min(float(fetch_timeout), remaining),
            socket_timeout=min(float(fetch_timeout), remaining),
            retry_attempts=retry_attempts,
            retry_backoff=retry_backoff,
            budget_left=_budget_left,
            heartbeat=_beat)
        if page_fetch.timed_out:
            errors.append(
                f"subpage {url}: fetch_timeout after {page_fetch.waited:.1f}s")
            continue
        if page_fetch.error is not None:
            error = page_fetch.error
            if not isinstance(error, Exception):
                raise error
            errors.append(
                f"subpage {url}: {type(error).__name__}: {error}")
            continue
        page_result = page_fetch.result
        if page_result is None:
            errors.append(f"subpage {url}: no fetch result")
            continue
        if not page_result.ok:
            errors.append(
                f"subpage {url}: {page_result.error_kind or 'fetch_failed'}")
            continue
        pages.append(parse_html(page_result.final_url or url,
                                page_result.body or ""))

    # --- final hard gate: SAME call, SAME arguments as the live pipeline ---
    final = classify_station_site(
        website_url=target,
        homepage_title=homepage_title,
        texts=(pages[0].text or "",),
        snippet="",
        secondary_texts=tuple(p.text or "" for p in pages[1:]),
    )

    # --- evidence-based geography only; unknown stays None ----------------
    loc = extract_location(url=target, title=homepage_title, pages=pages)
    classification = classify_station([p.text or "" for p in pages])

    return RequalificationOutcome(
        station_id=station.station_id,
        name=station.name,
        domain=station.domain,
        website=target,
        current_status=station.current_status,
        current_verdict=station.current_verdict,
        proposed_verdict=final.verdict,
        proposed_kind=final.kind,
        reason=final.reason,
        evidence=tuple(final.evidence),
        determinism=NETWORK_VERIFIED,
        pre_fetch_verdict=pre.verdict,
        pages_fetched=len(pages),
        proposed_country=loc.country,
        proposed_state_or_region=loc.state_or_region,
        proposed_city=loc.city,
        proposed_station_type=classification.station_type,
        errors=tuple(errors),
        evaluated_at=final.evaluated_at or utc_now_iso(),
    )


def requalify_all(
    stations: Iterable[ExistingStation],
    *,
    fetcher: Any | None = None,
    config: EngineConfig | None = None,
    on_record=None,
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    record_budget: float = DEFAULT_RECORD_BUDGET_SECONDS,
    heartbeat=None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    detector: OutageDetector | None = None,
) -> list[RequalificationOutcome]:
    """Re-evaluate many records. One failure never aborts the pass.

    A single hanging site is bounded by ``fetch_timeout`` inside
    :func:`requalify_record`, so this loop always advances.

    If ``detector`` is supplied and reports an outage, the loop stops early
    and the remaining records are never evaluated -- a truncated report that
    is honestly labelled beats a complete one built on a dead network.
    """
    outcomes: list[RequalificationOutcome] = []
    for station in stations:
        try:
            outcome = requalify_record(
                station, fetcher=fetcher, config=config,
                fetch_timeout=fetch_timeout, record_budget=record_budget,
                heartbeat=(lambda s, **kw: heartbeat(s, **kw))
                if heartbeat is not None else None,
                retry_attempts=retry_attempts, retry_backoff=retry_backoff)
        except Exception as exc:  # a bad record must not kill the report
            outcome = _empty_outcome(
                station, verdict=NEEDS_REVIEW, kind="runner_error",
                reason=f"{type(exc).__name__}: {exc}", errors=[repr(exc)],
                network_error_kind="runner_error")
        outcomes.append(outcome)
        if on_record is not None:
            on_record(outcome)
        if detector is not None and detector.record(outcome):
            print(f"\n{OUTAGE_ABORT_MESSAGE}", file=sys.stderr, flush=True)
            print(f"Stopped after {detector.samples} records: "
                  f"{detector.failure_rate:.0%} of the last "
                  f"{detector.window} failed on transient network errors "
                  f"(threshold {detector.threshold:.0%}).",
                  file=sys.stderr, flush=True)
            break
    return outcomes


def make_progress_printer(
    total: int,
    *,
    every: int = 1,
    stream: Any | None = None,
    label: str = "",
):
    """Return an ``on_record`` callback that prints live progress.

    Prints ``[n/total] elapsed verdict id kind`` to stderr so a run that is
    piped to a file still shows movement. ``every=0`` prints only a periodic
    keep-alive plus the final record.
    """
    out = stream if stream is not None else sys.stderr
    state = {"n": 0, "start": time.monotonic(), "last": 0.0}

    def _print(outcome: RequalificationOutcome) -> None:
        state["n"] += 1
        n, total_n = state["n"], max(0, int(total))
        now = time.monotonic()
        elapsed = now - state["start"]
        if every and every > 0 and n % every and n != total_n:
            return
        try:
            ident = outcome.domain or outcome.website or outcome.station_id
        except Exception:
            ident = outcome.station_id
        out.write(
            f"[{n}/{total_n or '?'}] {elapsed:7.1f}s "
            f"{outcome.proposed_verdict:<12} {str(ident)[:40]:<40} "
            f"{outcome.proposed_kind}{(' ' + label) if label else ''}\n")
        out.flush()

    return _print


# ---------------------------------------------------------------- the report


@dataclass
class RequalificationReport:
    """Aggregated dry-run report. Holds proposals only; writes nothing.

    ``aborted`` means the run was cut short by the network-outage detector.
    Such a report is NOT a trustworthy full-run result: ``render()`` and
    ``to_dict()`` both say so explicitly, and the CLI exits non-zero.
    """

    outcomes: list[RequalificationOutcome] = field(default_factory=list)
    generated_at: str = ""
    mode: str = "offline"
    aborted: bool = False
    abort_reason: str = ""
    records_total: int = 0

    # ---------------------------------------------------------------- totals
    @property
    def inspected(self) -> int:
        return len(self.outcomes)

    @property
    def totals(self) -> dict[str, int]:
        counts = Counter(o.proposed_verdict for o in self.outcomes)
        return {bucket: int(counts.get(bucket, 0)) for bucket in BUCKETS}

    @property
    def deterministic_garbage(self) -> list[RequalificationOutcome]:
        return [o for o in self.outcomes if o.is_deterministic_garbage]

    @property
    def needs_network_verification(self) -> list[RequalificationOutcome]:
        return [o for o in self.outcomes if o.needs_network_verification]

    @property
    def changes(self) -> list[RequalificationOutcome]:
        """Records whose proposed verdict differs from what is stored."""
        return [o for o in self.outcomes
                if o.proposed_verdict != (o.current_verdict or "none")]

    @property
    def is_trustworthy(self) -> bool:
        """False when the run aborted; partial results must not be trusted."""
        return not self.aborted

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at or utc_now_iso(),
            "mode": self.mode,
            "dry_run": True,
            "aborted": self.aborted,
            "trustworthy": self.is_trustworthy,
            "abort_reason": self.abort_reason,
            "records_total": self.records_total or self.inspected,
            "inspected": self.inspected,
            "totals": self.totals,
            "deterministic_garbage": len(self.deterministic_garbage),
            "needs_network_verification": len(self.needs_network_verification),
            "changed_vs_stored": len(self.changes),
            "records": [o.to_dict() for o in self.outcomes],
        }

    # ----------------------------------------------------------------- text
    def render(self) -> str:
        lines: list[str] = []
        add = lines.append
        add("=" * 100)
        if self.aborted:
            add("STATION RE-QUALIFICATION -- ABORTED, DO NOT TRUST THIS REPORT")
        else:
            add("STATION RE-QUALIFICATION -- DRY RUN (read-only; nothing written)")
        add("=" * 100)
        add(f"generated_at : {self.generated_at or utc_now_iso()}")
        add(f"mode         : {self.mode}")
        add(f"inspected    : {self.inspected} existing station records")
        if self.aborted:
            add("")
            add("!" * 100)
            add(f"ABORTED: {self.abort_reason or OUTAGE_ABORT_MESSAGE}")
            add("This run stopped early because the network, not the stations, "
                "was failing.")
            add(f"Only {self.inspected} of {self.records_total or self.inspected} "
                "records were evaluated.")
            add("The counts below describe the partial run and MUST NOT be used "
                "as requalification decisions.")
            add("Re-run when connectivity is stable.")
            add("!" * 100)
        add("")
        if not self.aborted:
            add("PROPOSED TOTALS")
        for bucket in BUCKETS:
            add(f"  {bucket:<14} {self.totals[bucket]:>5}")
        add("")
        add("SUBSETS")
        add(f"  deterministic garbage (no network needed) "
            f"{len(self.deterministic_garbage):>5}")
        add(f"  needs live network verification           "
            f"{len(self.needs_network_verification):>5}")
        add(f"  proposed verdict differs from stored      "
            f"{len(self.changes):>5}")
        add("")

        add("=" * 100)
        add("PER-RECORD PROPOSALS")
        add("=" * 100)
        add(f"{'station id':<26} {'domain':<26} {'current qual':<14} "
            f"{'current status':<15} {'proposed':<13} {'kind':<26} "
            f"{'det':<10} name")
        add("-" * 100)
        for o in self.outcomes:
            add(f"{o.station_id:<26} {(o.domain or '-')[:25]:<26} "
                f"{(o.current_label or 'none')[:13]:<14} "
                f"{(o.current_status or '-')[:14]:<15} "
                f"{o.proposed_verdict:<13} {o.proposed_kind[:25]:<26} "
                f"{o.determinism[:9]:<10} {(o.name or '')[:30]}")
            add(f"    reason : {o.reason}")
            add(f"    evidence: {', '.join(o.evidence) or '(none)'}")
            if o.errors:
                add(f"    errors : {'; '.join(o.errors)}")
        add("")

        for title, rows in (
            ("DETERMINISTIC GARBAGE (decided with no network)", self.deterministic_garbage),
            ("REQUIRES LIVE NETWORK VERIFICATION", self.needs_network_verification),
        ):
            add("=" * 100)
            add(f"{title} ({len(rows)})")
            add("=" * 100)
            for o in rows:
                add(f"  {o.station_id:<26} {(o.domain or '-'):<26} "
                    f"{o.proposed_verdict:<13} {o.proposed_kind:<26} "
                    f"{(o.name or '')[:28]}")
            add("")
        return "\n".join(lines)


def build_report(
    stations: Iterable[ExistingStation],
    *,
    fetcher: Any | None = None,
    config: EngineConfig | None = None,
    mode: str = "offline",
    on_record=None,
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    record_budget: float = DEFAULT_RECORD_BUDGET_SECONDS,
    heartbeat=None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    detector: OutageDetector | None = None,
) -> RequalificationReport:
    """Run the pass. If ``detector`` aborts, the report is marked untrusted."""
    outcomes = requalify_all(
        stations, fetcher=fetcher, config=config, on_record=on_record,
        fetch_timeout=fetch_timeout, record_budget=record_budget,
        heartbeat=heartbeat, retry_attempts=retry_attempts,
        retry_backoff=retry_backoff, detector=detector)
    report = RequalificationReport(outcomes=outcomes,
                                   generated_at=utc_now_iso(), mode=mode)
    if detector is not None and detector.aborted:
        report.aborted = True
        report.abort_reason = (
            f"{OUTAGE_ABORT_MESSAGE} "
            f"({detector.failure_rate:.0%} of the last {detector.window} "
            f"records failed on transient network errors, above the "
            f"{detector.threshold:.0%} threshold)")
        report.records_total = detector.records_total
    return report


# -------------------------------------------------------- read-only DB read


def load_existing_stations(
    dsn: str,
    *,
    dry_run: bool,
    organization_type: str = "radio_station",
    limit: int | None = None,
) -> list[ExistingStation]:
    """READ existing station records. SELECT only; rolled back.

    ``dry_run`` must be True: this function has no other mode. The
    connection is opened ``read_only=True`` and rolled back before it is
    closed, so the call cannot mutate production even by accident.
    """
    if not dry_run:
        raise ReadOnlyViolation(
            "load_existing_stations is read-only: dry_run=True is required. "
            "This tool has no write path.")
    import psycopg
    from psycopg.rows import dict_row

    sql = """
      SELECT identity_key, name, domain, website, status, station_type,
             country, state_or_region, city,
             first_stored_at, last_stored_at,
             raw_metadata
      FROM organizations
      WHERE organization_type = %s
      ORDER BY identity_key
    """
    params: list[Any] = [organization_type]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))

    stations: list[ExistingStation] = []
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                meta = row["raw_metadata"]
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except ValueError:
                        meta = {}
                meta = meta or {}
                qual = meta.get("qualification") or {}
                if not isinstance(qual, dict):
                    qual = {}
                stations.append(ExistingStation(
                    station_id=row["identity_key"],
                    name=row["name"],
                    domain=row["domain"],
                    website=row["website"],
                    current_status=row["status"],
                    current_verdict=(qual.get("verdict") or None),
                    current_kind=(qual.get("kind") or None),
                    current_reason=(qual.get("reason") or None),
                    current_homepage_title=(meta.get("homepage_title") or None),
                    current_country=row["country"],
                    current_state_or_region=row["state_or_region"],
                    current_city=row["city"],
                    current_station_type=row["station_type"],
                    first_stored_at=row["first_stored_at"],
                    last_stored_at=row["last_stored_at"],
                ))
        conn.rollback()
    return stations


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="discovery.radio.requalify",
        description=(
            "Re-evaluate already-stored station records against the current "
            "qualification rules. DRY RUN ONLY: this tool has no write path."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", required=False,
                        help="required; confirms no writes will occur")
    parser.add_argument("--dsn-env", default="MIE_PG_DSN",
                        help="env var holding the Postgres DSN")
    parser.add_argument("--offline", action="store_true",
                        help="do not fetch; report only what is decidable "
                             "without network access")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--report-out", default=None,
                        help="optional path for the JSON report")
    parser.add_argument("--fetch-timeout", type=float,
                        default=DEFAULT_FETCH_TIMEOUT_SECONDS,
                        help="hard per-site wall-clock cap in seconds "
                             f"(default {DEFAULT_FETCH_TIMEOUT_SECONDS:g}); "
                             "a site that exceeds it is reported "
                             "needs_review/fetch_timeout and the run moves on")
    parser.add_argument("--record-budget", type=float,
                        default=DEFAULT_RECORD_BUDGET_SECONDS,
                        help="total wall-clock cap per record in seconds "
                             f"(default {DEFAULT_RECORD_BUDGET_SECONDS:g})")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRY_ATTEMPTS,
                        help="retries for TRANSIENT failures only (dns_error, "
                             "connection_error, HTTP 429/503) "
                             f"(default {DEFAULT_RETRY_ATTEMPTS}; 2 = 3 calls)")
    parser.add_argument("--retry-backoff", type=float,
                        default=DEFAULT_RETRY_BACKOFF_SECONDS,
                        help="seconds between transient-failure retries "
                             f"(default {DEFAULT_RETRY_BACKOFF_SECONDS:g})")
    parser.add_argument("--outage-window", type=int,
                        default=DEFAULT_OUTAGE_WINDOW,
                        help="rolling window of records used to detect a "
                             f"network outage (default {DEFAULT_OUTAGE_WINDOW})")
    parser.add_argument("--outage-threshold", type=float,
                        default=DEFAULT_OUTAGE_FAILURE_RATIO,
                        help="abort if MORE than this fraction of the window "
                             "failed with a TRANSIENT NETWORK error "
                             "(dns_error, connection_error, HTTP 429/503). "
                             "Slow sites and robots.txt refusals do NOT "
                             "count. Default "
                             f"{DEFAULT_OUTAGE_FAILURE_RATIO:g} = "
                             f"{DEFAULT_OUTAGE_FAILURE_RATIO * 100:.0f}%%)")
    parser.add_argument("--no-outage-guard", action="store_true",
                        help="disable the network-outage abort (not advised)")
    parser.add_argument("--progress-every", type=int, default=1,
                        help="print a progress line every N records; "
                             "0 = only the last record (default: 1)")
    args = parser.parse_args(argv)

    if not args.dry_run:
        print("--dry-run is required: this tool only ever produces a report.",
              file=sys.stderr)
        return 2

    dsn = os.environ.get(args.dsn_env, "")
    if not dsn:
        print(f"env var {args.dsn_env} is not set", file=sys.stderr)
        return 2

    stations = load_existing_stations(dsn, dry_run=True, limit=args.limit)

    fetch_timeout = max(0.1, float(args.fetch_timeout))
    record_budget = max(fetch_timeout, float(args.record_budget))

    fetcher = None
    mode = "offline (no fetches; network-dependent records reported)"
    if not args.offline:
        fetcher = StdlibHttpFetcher(
            # Kept in step with our own wall-clock cap: the shared client
            # bounds each socket op, and fetch_within() bounds the whole call.
            timeout_seconds=fetch_timeout,
            rate_limit_seconds=1.0,
            respect_robots=True,
            user_agent=DEFAULT_USER_AGENT)
        mode = "live fetch (read-only; still no writes)"

    total = len(stations)
    progress_every = max(0, int(args.progress_every))
    print(f"requalifying {total} existing records (read-only); "
          f"per-site cap {fetch_timeout:g}s, per-record cap {record_budget:g}s",
          file=sys.stderr, flush=True)
    progress = make_progress_printer(
        total, every=progress_every, stream=sys.stderr,
        label=f"(cap {fetch_timeout:g}s)")

    def _beat(station, *, url, waited, remaining, fetch_index, elapsed) -> None:
        print(f"    ...still fetching {str(station.domain or station.website or station.station_id)[:40]}"
              f" #{fetch_index} {waited:.0f}s (site cap {remaining:.0f}s left,"
              f" record {elapsed:.0f}s in)", file=sys.stderr, flush=True)

    # A run that aborts must never leave behind a report that looks complete.
    detector = None
    if not args.offline and not args.no_outage_guard:
        detector = OutageDetector(window=args.outage_window,
                                  threshold=args.outage_threshold,
                                  records_total=total)

    report = build_report(
        stations, fetcher=fetcher, mode=mode, on_record=progress,
        fetch_timeout=fetch_timeout, record_budget=record_budget,
        heartbeat=None if args.offline else _beat,
        retry_attempts=max(0, int(args.retries)),
        retry_backoff=max(0.0, float(args.retry_backoff)),
        detector=detector)

    if report.aborted:
        print(f"\n{OUTAGE_ABORT_MESSAGE}", file=sys.stderr, flush=True)
        print(report.render())
        if args.report_out:
            # Still write the file, but marked untrustworthy, so a stale
            # complete-looking report cannot be mistaken for this run.
            with open(args.report_out, "w", encoding="utf-8") as handle:
                json.dump(report.to_dict(), handle, indent=2,
                          ensure_ascii=True)
            print(f"\nABORTED: wrote an explicitly untrustworthy report to "
                  f"{args.report_out}. Do not use it for decisions.",
                  file=sys.stderr, flush=True)
        return 3

    print(f"done: evaluated {len(report.outcomes)}/{total} records "
          f"(see progress lines above)", file=sys.stderr, flush=True)
    print(report.render())

    if args.report_out:
        with open(args.report_out, "w", encoding="utf-8") as handle:
            json.dump(report.to_dict(), handle, indent=2, ensure_ascii=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry
    sys.exit(main())
