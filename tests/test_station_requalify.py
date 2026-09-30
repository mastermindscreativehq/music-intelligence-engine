"""Focused tests for the one-time station re-qualification runner.

The runner is a dry-run shell over the EXISTING qualification engine, so
these tests assert three things:

1. it reuses the current rules rather than re-deciding anything
   (article-path precedence, first-person self-identification, sub-pages
   cannot promote);
2. it is read-only by construction -- no ingest path is imported, no
   repository is ever accepted, and the one DB helper refuses any mode but
   a rolled-back read;
3. it separates deterministic garbage from records that genuinely need a
   live network verification, and preserves unknown values instead of
   inventing them.
"""

import inspect
import io
import socket
import ssl
import threading
import time
import unittest

from crawler.http import FetchResult
from discovery.radio.requalify import (
    DETERMINISTIC,
    NETWORK_REQUIRED,
    NETWORK_VERIFIED,
    OUTAGE_ABORT_MESSAGE,
    RETRYABLE_ERROR_KINDS,
    RETRYABLE_HTTP_STATUS,
    DEFAULT_OUTAGE_FAILURE_RATIO,
    DEFAULT_OUTAGE_WINDOW,
    DEFAULT_RETRY_ATTEMPTS,
    ReadOnlyViolation,
    ExistingStation,
    OutageDetector,
    build_report,
    classify_transient_outcome,
    fetch_with_retries,
    fetch_within,
    is_transient_network_failure,
    load_existing_stations,
    main,
    make_progress_printer,
    requalify_all,
    requalify_record,
)
import discovery.radio.requalify as requalify_mod


# ------------------------------------------------------------------- fixtures

GENUINE_HOME = (
    "<html><head><title>KQXR 101.5 FM Cedar Valley</title></head><body>"
    "<p>We broadcast 24 hours a day from Cedar Valley. Listen live on "
    "101.5 FM.</p><a href=\"/about\">About us</a></body></html>"
)
GENUINE_ABOUT = (
    "<html><head><title>About KQXR</title></head><body>"
    "<p>Our frequency is 101.5 FM. We are on the air.</p></body></html>"
)
ARTICLE_HOME = (
    "<html><head><title>Real College Radio Is A Real Opportunity</title></head>"
    "<body><p>KCPR 91.7 FM is a college radio station that broadcasts from "
    "the University of Minnesota Twin Cities.</p></body></html>"
)
HOMEPAGE_NO_EVIDENCE = (
    "<html><head><title>Community Media Collective</title></head><body>"
    "<p>Welcome to our project.</p>"
    "<a href=\"/about/station\">About the station</a></body></html>"
)
SUBPAGE_FULL_EVIDENCE = (
    "<html><head><title>Our Station</title></head><body>"
    "<p>Our frequency is 101.5 FM. We broadcast from Cedar Valley and we "
    "are on the air. Listen live.</p></body></html>"
)


class FakeFetcher:
    """Serves canned HTML; records every call so tests can prove intent."""

    def __init__(self, pages=None, *, fail_urls=(), raise_urls=()):
        self.pages = dict(pages or {})
        self.fail_urls = set(fail_urls)
        self.raise_urls = set(raise_urls)
        self.calls: list[str] = []

    def fetch(self, url, **_kwargs):
        self.calls.append(url)
        if url in self.raise_urls:
            raise RuntimeError("simulated fetcher explosion")
        if url in self.fail_urls:
            return FetchResult(url=url, error_kind="http_status",
                               error_message="404")
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url=url, error_kind="dns_error",
                               error_message="no such host")
        return FetchResult(url=url, final_url=url, status=200,
                           content_type="text/html", body=body)


def station(**kwargs) -> ExistingStation:
    base = dict(
        station_id="domain:example.org",
        name="Example Radio",
        domain="example.org",
        website="https://example.org/",
        current_status="enriched",
        current_verdict="qualified",
    )
    base.update(kwargs)
    return ExistingStation(**base)


class HangingFetcher:
    """A fetcher that never returns, like the real run that had to be killed.

    It accepts and ignores the ``timeout=`` kwarg on purpose: that is exactly
    the behaviour of a socket whose ``read()`` keeps trickling bytes, which is
    how the live run wedged. Only a wall-clock cap above this layer can save
    it, so these tests prove the cap works even when the timeout is ignored.
    """

    def __init__(self, hang_urls=(), delay=30.0, pages=None):
        self.hang_urls = set(hang_urls)
        self.delay = delay
        self.pages = dict(pages or {})
        self.calls: list[str] = []
        self.saw_timeout_kwarg: list[float | None] = []

    def fetch(self, url, *, timeout=None, **_kwargs):
        self.calls.append(url)
        self.saw_timeout_kwarg.append(timeout)
        if url in self.hang_urls:
            time.sleep(self.delay)  # ignores `timeout`, as a real socket does
            return FetchResult(url=url, final_url=url, status=200,
                               content_type="text/html", body=GENUINE_HOME)
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url=url, error_kind="dns_error",
                               error_message="no such host")
        return FetchResult(url=url, final_url=url, status=200,
                           content_type="text/html", body=body)


class ExplodingFetcher:
    """Raises a transport-level error out of ``fetch`` on the first URL."""

    def __init__(self, error, pages=None, always_raise=False):
        self.error = error
        self.pages = dict(pages or {})
        self.always_raise = always_raise
        self.calls: list[str] = []

    def fetch(self, url, **_kwargs):
        self.calls.append(url)
        if self.always_raise or not self.calls[:-1]:  # homepage (or all)
            raise self.error
        body = self.pages.get(url, GENUINE_ABOUT)
        return FetchResult(url=url, final_url=url, status=200,
                           content_type="text/html", body=body)


# ------------------------------------------------------------------ the rules


class ReusesCurrentQualificationRulesTests(unittest.TestCase):
    """The runner must not be a second, looser qualification engine."""

    def test_guarded_fetch_still_reuses_the_engine_for_good_sites(self):
        """The new bounding must not change any decision for a normal site."""
        fetcher = FakeFetcher(pages={
            "https://example.org/": GENUINE_HOME,
            "https://example.org/about": GENUINE_ABOUT,
        })
        out = requalify_record(station(), fetcher=fetcher,
                               fetch_timeout=5.0, record_budget=30.0)
        self.assertEqual(out.proposed_verdict, "qualified")

    def test_article_path_is_rejected_without_any_fetch(self):
        out = requalify_record(station(
            website="https://twin-cities.umn.edu/news-events/real-college-radio",
            domain="umn.edu"))
        self.assertEqual(out.proposed_verdict, "rejected")
        self.assertEqual(out.proposed_kind, "non_station_path")
        self.assertEqual(out.determinism, DETERMINISTIC)

    def test_locale_prefixed_article_path_is_rejected(self):
        out = requalify_record(station(
            website=("https://rode.com/en-int/about/news-info/"
                     "they-killed-local-radio"),
            domain="rode.com"))
        self.assertEqual(out.proposed_verdict, "rejected")
        self.assertEqual(out.determinism, DETERMINISTIC)

    def test_article_body_about_a_station_is_not_promoted(self):
        # A station-named article reached via a non-article URL still must not
        # promote: third-person reference is not self-identification.
        fetcher = FakeFetcher({"https://example.org/": ARTICLE_HOME})
        out = requalify_record(station(), fetcher=fetcher)
        self.assertNotEqual(out.proposed_verdict, "qualified")
        self.assertEqual(out.proposed_verdict, "needs_review")

    def test_first_person_self_identification_is_required(self):
        fetcher = FakeFetcher({"https://example.org/": GENUINE_HOME})
        out = requalify_record(station(
            current_verdict="needs_review"), fetcher=fetcher)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertEqual(out.determinism, NETWORK_VERIFIED)

    def test_sub_pages_cannot_promote_a_candidate(self):
        fetcher = FakeFetcher({
            "https://example.org/": HOMEPAGE_NO_EVIDENCE,
            "https://example.org/about/station": SUBPAGE_FULL_EVIDENCE,
        })
        out = requalify_record(station(), fetcher=fetcher)
        self.assertNotEqual(out.proposed_verdict, "qualified")
        self.assertEqual(out.proposed_verdict, "needs_review")
        # The sub-page WAS fetched and still could only inform, never promote.
        self.assertIn("https://example.org/about/station", fetcher.calls)
        self.assertTrue(any(e.startswith("subpage:")
                            for e in out.evidence), out.evidence)

    def test_genuine_station_homepage_still_qualifies(self):
        fetcher = FakeFetcher({
            "https://example.org/": GENUINE_HOME,
            "https://example.org/about": GENUINE_ABOUT,
        })
        out = requalify_record(station(), fetcher=fetcher)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertGreaterEqual(out.pages_fetched, 1)

    def test_denied_host_rejected_after_fetch(self):
        # npr.org is on the post-fetch deny list; the pre-fetch gate lets it
        # through, so this exercises the EARLY hard gate specifically.
        # NOTE: normalize_url() strips "www", so the fake site is keyed on
        # the normalized URL the runner actually requests.
        out = requalify_record(station(
            station_id="domain:npr.org", domain="npr.org",
            website="https://www.npr.org/segment"),
            fetcher=FakeFetcher({"https://npr.org/segment": ARTICLE_HOME}))
        self.assertEqual(out.proposed_verdict, "rejected")
        self.assertEqual(out.proposed_kind, "denied_host")
        self.assertEqual(out.determinism, NETWORK_VERIFIED)

    def test_stored_title_alone_never_promotes_without_a_fetch(self):
        # A stored title full of station signals gets past the PRE-fetch
        # gate, but the final verdict still needs the site's own pages.
        out = requalify_record(station(
            current_homepage_title="KQXR 101.5 FM Cedar Valley Radio"))
        self.assertEqual(out.proposed_verdict, "needs_review")
        self.assertEqual(out.determinism, NETWORK_REQUIRED)
        self.assertEqual(out.proposed_kind, "unverified_pending_live_fetch")


class EvidenceDisciplineTests(unittest.TestCase):
    """No discovery-query evidence, no carried geography, no invention."""

    def test_stored_geography_is_not_reused_as_evidence(self):
        # Stored geography must never be fed back in as a source. The stored
        # values are deliberately ones the site's own pages/domain cannot
        # produce, so any echo would mean carry-through.
        fetcher = FakeFetcher({"https://example.org/": GENUINE_HOME})
        out = requalify_record(station(
            current_country="Iceland",
            current_state_or_region="Sudurnes",
            current_city="Reykjavik"), fetcher=fetcher)
        stored = {"Iceland", "Sudurnes", "Reykjavik"}
        for value in (out.proposed_country, out.proposed_state_or_region,
                      out.proposed_city):
            self.assertNotIn(value, stored)

    def test_unknown_geography_stays_unknown(self):
        # Only fields with real site evidence are filled; the rest stay None
        # rather than being invented or carried over.
        fetcher = FakeFetcher({"https://example.org/": GENUINE_HOME})
        out = requalify_record(station(), fetcher=fetcher)
        self.assertIsNone(out.proposed_state_or_region)
        self.assertIsNone(out.proposed_city)

    def test_unreachable_site_is_quarantined_not_called_garbage(self):
        out = requalify_record(station(), fetcher=FakeFetcher())
        self.assertEqual(out.proposed_verdict, "needs_review")
        self.assertEqual(out.proposed_kind, "fetch_failed")
        self.assertEqual(out.determinism, NETWORK_REQUIRED)
        self.assertFalse(out.is_deterministic_garbage)

    def test_record_without_any_url_is_reported_honestly(self):
        out = requalify_record(station(website=None, domain=None))
        self.assertEqual(out.proposed_verdict, "needs_review")
        self.assertEqual(out.proposed_kind, "unusable_target")

    def test_domain_only_record_falls_back_to_https(self):
        out = requalify_record(station(website=None, domain="example.org"))
        self.assertEqual(out.website, "https://example.org")


class ReadOnlyByConstructionTests(unittest.TestCase):
    """The dry run must be incapable of mutating production."""

    def test_module_never_imports_the_ingest_write_path(self):
        # Inspect the real import graph, not the prose: the docstring NAMES
        # the ingest path in order to document that it is NOT used.
        import ast

        import discovery.radio.requalify as module
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        for forbidden in ("ingest_station_discovery", "ingest_intelligence",
                          "stations", "stations.service", "database",
                          "repository"):
            self.assertNotIn(forbidden, imported,
                             f"requalify must not import {forbidden}")

    def test_module_issues_no_write_sql(self):
        import ast

        import discovery.radio.requalify as module
        source = inspect.getsource(module)
        tree = ast.parse(source)
        statements: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(
                    node.value, ast.Constant) and isinstance(
                    node.value.value, str):
                statements.append(node.value.value)
        for sql in statements:
            for write in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
                          "CREATE", "TRUNCATE"):
                self.assertNotIn(
                    write, sql.upper(),
                    f"requalify must not contain {write} SQL: {sql[:60]!r}")

    def test_runner_signature_accepts_no_write_capable_object(self):
        """Only passive inputs -- no repository, connection or service."""
        params = set(inspect.signature(requalify_record).parameters)
        self.assertEqual(params, {"station", "fetcher", "config",
                                  "fetch_timeout", "record_budget",
                                  "heartbeat", "retry_attempts",
                                  "retry_backoff"})
        for name in ("fetch_timeout", "record_budget", "retry_attempts",
                     "retry_backoff"):
            default = inspect.signature(
                requalify_record).parameters[name].default
            self.assertIsInstance(default, (int, float),
                                  "bounds must be inert numbers, not handles")

    def test_fetcher_failure_on_one_record_does_not_abort_the_pass(self):
        stations = [
            station(station_id="domain:boom.org", domain="boom.org",
                    website="https://boom.org/"),
            station(station_id="domain:ok.org", domain="ok.org",
                    website="https://ok.org/"),
        ]
        fetcher = FakeFetcher({"https://ok.org/": GENUINE_HOME},
                              raise_urls={"https://boom.org/"})
        outcomes = requalify_all(stations, fetcher=fetcher)
        self.assertEqual(len(outcomes), 2)
        # A raising fetcher is caught at the fetch boundary and reported as
        # needs_review; it is never treated as a rejected station.
        self.assertEqual(outcomes[0].proposed_verdict, "needs_review")
        self.assertIn(outcomes[0].proposed_kind, {"fetch_error",
                                                  "runner_error"})
        # The failure does not stop or contaminate the next record.
        self.assertEqual(outcomes[1].proposed_verdict, "qualified")

    def test_load_existing_stations_refuses_any_non_dry_run_mode(self):
        # The guard fires BEFORE any connection is attempted, so this test
        # never touches a database.
        with self.assertRaises(ReadOnlyViolation):
            load_existing_stations("postgresql://unused", dry_run=False)

    def test_cli_refuses_to_run_without_dry_run(self):
        self.assertEqual(main([]), 2)

    def test_cli_reports_missing_dsn_instead_of_connecting(self):
        import os
        previous = os.environ.pop("MIE_PG_DSN", None)
        try:
            self.assertEqual(main(["--dry-run"]), 2)
        finally:
            if previous is not None:
                os.environ["MIE_PG_DSN"] = previous


class ReportTests(unittest.TestCase):
    """The report is the deliverable: totals plus the two required subsets."""

    def setUp(self):
        self.stations = [
            station(station_id="domain:garbage-article.org",
                    name="Campus News Story", domain="garbage-article.org",
                    website="https://garbage-article.org/news/real-college-radio",
                    current_verdict="qualified"),
            station(station_id="domain:good.org", name="Good Radio",
                    domain="good.org", website="https://good.org/",
                    current_verdict="qualified"),
            station(station_id="domain:unknown.org", name="Unknown Radio",
                    domain="unknown.org", website="https://unknown.org/",
                    current_verdict="none"),
        ]
        self.fetcher = FakeFetcher({"https://good.org/": GENUINE_HOME})
        self.report = build_report(self.stations, fetcher=self.fetcher,
                                   mode="test")

    def test_every_record_lands_in_one_of_three_buckets(self):
        for outcome in self.report.outcomes:
            self.assertIn(outcome.proposed_verdict,
                          ("qualified", "needs_review", "rejected"))

    def test_totals_cover_every_inspected_record(self):
        self.assertEqual(self.report.inspected, 3)
        self.assertEqual(sum(self.report.totals.values()), 3)

    def test_deterministic_garbage_subset(self):
        garbage = self.report.deterministic_garbage
        self.assertEqual([o.station_id for o in garbage],
                         ["domain:garbage-article.org"])
        self.assertEqual(garbage[0].proposed_verdict, "rejected")
        # Deterministic garbage must not have needed the network.
        self.assertNotIn("https://garbage-article.org/news/real-college-radio",
                         self.fetcher.calls)

    def test_network_verification_subset(self):
        pending = self.report.needs_network_verification
        self.assertEqual([o.station_id for o in pending],
                         ["domain:unknown.org"])

    def test_network_verified_records_are_not_in_either_subset(self):
        verified = [o for o in self.report.outcomes
                    if o.determinism == NETWORK_VERIFIED]
        self.assertEqual([o.station_id for o in verified],
                         ["domain:good.org"])

    def test_changes_against_stored_verdict(self):
        # Two genuine changes: a stored "qualified" article -> rejected, and
        # a record with no stored verdict -> needs_review.
        changed = {o.station_id for o in self.report.changes}
        self.assertEqual(changed, {"domain:garbage-article.org",
                                   "domain:unknown.org"})

    def test_render_includes_every_required_column(self):
        text = self.report.render()
        for needle in ("station id", "current qual", "current status",
                       "proposed", "reason", "evidence",
                       "DETERMINISTIC GARBAGE",
                       "REQUIRES LIVE NETWORK VERIFICATION"):
            self.assertIn(needle, text)
        for outcome in self.report.outcomes:
            self.assertIn(outcome.station_id, text)
            self.assertIn(outcome.proposed_verdict, text)

    def test_to_dict_is_serialisable_and_marks_dry_run(self):
        payload = self.report.to_dict()
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["inspected"], 3)
        self.assertEqual(len(payload["records"]), 3)
        self.assertIn("deterministic_garbage", payload)
        self.assertIn("needs_network_verification", payload)

    def test_offline_report_marks_survivors_as_needing_network(self):
        report = build_report(self.stations, fetcher=None, mode="offline")
        self.assertEqual(report.totals["needs_review"], 2)
        self.assertEqual(len(report.deterministic_garbage), 1)
        self.assertEqual(len(report.needs_network_verification), 2)


# ------------------------------------------------- resilience to dead sites


class HangingSiteResilienceTests(unittest.TestCase):
    """One slow/dead site must never stall or skew the run.

    These reproduce the failure that stopped the live pass: a fetch that never
    returns. The runner has to bound it, label it honestly, and move on.
    """

    def test_hanging_fetch_becomes_needs_review_not_rejected(self):
        fetcher = HangingFetcher(hang_urls={"https://slow.org/"})
        started = time.monotonic()
        out = requalify_record(
            station(station_id="domain:slow.org", domain="slow.org",
                    website="https://slow.org/"),
            fetcher=fetcher, fetch_timeout=0.4, record_budget=5.0)
        elapsed = time.monotonic() - started

        # Bounded: we did NOT wait out the 30s hang.
        self.assertLess(elapsed, 10.0)
        self.assertEqual(out.proposed_verdict, "needs_review")
        self.assertEqual(out.proposed_kind, "fetch_timeout")
        # An unreachable site is never proof of a non-station.
        self.assertNotEqual(out.proposed_verdict, "rejected")
        self.assertEqual(out.determinism, NETWORK_REQUIRED)

    def test_hanging_fetch_reason_is_actionable(self):
        fetcher = HangingFetcher(hang_urls={"https://slow.org/"})
        out = requalify_record(
            station(station_id="domain:slow.org", domain="slow.org",
                    website="https://slow.org/"),
            fetcher=fetcher, fetch_timeout=0.4, record_budget=5.0)
        self.assertIn("slow.org", out.reason)
        self.assertIn("0.4s", out.reason)
        self.assertTrue(any("fetch_timeout" in e for e in out.errors))

    def test_record_budget_caps_total_time_including_subpages(self):
        # Homepage resolves; a sub-page hangs. Without the record budget the
        # sub-page loop would keep going at max_pages fetches long.
        fetcher = HangingFetcher(
            hang_urls={"https://budget.org/about/station"},
            pages={"https://budget.org/": HOMEPAGE_NO_EVIDENCE})
        started = time.monotonic()
        out = requalify_record(
            station(station_id="domain:budget.org", domain="budget.org",
                    website="https://budget.org/"),
            fetcher=fetcher, fetch_timeout=0.4, record_budget=1.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0)
        # A lost sub-page can never promote a record.
        self.assertNotEqual(out.proposed_verdict, "qualified")
        self.assertTrue(any("fetch_timeout" in e or "budget" in e
                            for e in out.errors))

    def test_processing_continues_after_a_hanging_station(self):
        """The core requirement: neighbours are still evaluated afterwards."""
        stations = [
            station(station_id="domain:a.org", domain="a.org",
                    website="https://a.org/"),
            station(station_id="domain:hang.org", domain="hang.org",
                    website="https://hang.org/"),
            station(station_id="domain:c.org", domain="c.org",
                    website="https://c.org/"),
        ]
        fetcher = HangingFetcher(
            hang_urls={"https://hang.org/"},
            pages={"https://a.org/": GENUINE_HOME,
                   "https://a.org/about": GENUINE_ABOUT,
                   "https://c.org/": GENUINE_HOME,
                   "https://c.org/about": GENUINE_ABOUT})

        seen: list[str] = []
        outcomes = requalify_all(
            stations, fetcher=fetcher, fetch_timeout=0.4, record_budget=5.0,
            on_record=lambda o: seen.append(o.station_id))

        # All three were evaluated, in order, despite the middle one hanging.
        self.assertEqual(seen, ["domain:a.org", "domain:hang.org",
                                "domain:c.org"])
        self.assertEqual(len(outcomes), 3)
        by_id = {o.station_id: o for o in outcomes}
        # The neighbours are unaffected by the failure.
        self.assertEqual(by_id["domain:a.org"].proposed_verdict, "qualified")
        self.assertEqual(by_id["domain:c.org"].proposed_verdict, "qualified")
        self.assertEqual(by_id["domain:hang.org"].proposed_kind, "fetch_timeout")
        # And exactly one record is quarantined, not a cascade.
        quarantined = [o for o in outcomes
                       if o.proposed_verdict == "needs_review"]
        self.assertEqual(len(quarantined), 1)

    def test_transport_exception_becomes_needs_review_fetch_error(self):
        for error in (socket.timeout("timed out"),
                      ConnectionResetError("reset by peer"),
                      ssl.SSLError("handshake failure"),
                      OSError("network unreachable")):
            with self.subTest(error=type(error).__name__):
                out = requalify_record(
                    station(station_id="domain:boom.org", domain="boom.org",
                            website="https://boom.org/"),
                    fetcher=ExplodingFetcher(error),
                    fetch_timeout=2.0, record_budget=5.0)
                self.assertEqual(out.proposed_verdict, "needs_review")
                self.assertEqual(out.proposed_kind, "fetch_error")
                self.assertNotEqual(out.proposed_verdict, "rejected")
                self.assertIn(type(error).__name__, out.reason)

    def test_run_survives_an_exception_on_every_record(self):
        """A fetcher that raises on every call still yields a full report."""
        stations = [station(station_id=f"domain:{i}.org",
                            domain=f"{i}.org", website=f"https://{i}.org/")
                    for i in range(3)]
        outcomes = requalify_all(
            stations, fetcher=ExplodingFetcher(RuntimeError("boom"),
                                               always_raise=True),
            fetch_timeout=2.0, record_budget=5.0)
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(o.proposed_verdict == "needs_review"
                            for o in outcomes))
        self.assertTrue(all(o.proposed_kind == "fetch_error"
                            for o in outcomes))
        self.assertTrue(all(o.proposed_verdict != "rejected"
                            for o in outcomes))

    def test_socket_timeout_is_passed_down_to_the_fetcher(self):
        """We use the fetcher's own mechanism too, not only our own cap."""
        fetcher = HangingFetcher(pages={"https://example.org/": GENUINE_HOME,
                                        "https://example.org/about":
                                        GENUINE_ABOUT})
        requalify_record(station(), fetcher=fetcher,
                         fetch_timeout=7.5, record_budget=30.0)
        self.assertTrue(fetcher.saw_timeout_kwarg)
        self.assertTrue(all(t == 7.5 for t in fetcher.saw_timeout_kwarg))

    def test_fetcher_without_timeout_kwarg_still_works(self):
        class OldFetcher:
            def __init__(self):
                self.calls = []

            def fetch(self, url):  # no **kwargs at all
                self.calls.append(url)
                if url == "https://example.org/":
                    return FetchResult(url=url, final_url=url, status=200,
                                       content_type="text/html",
                                       body=GENUINE_HOME)
                return FetchResult(url=url, final_url=url, status=200,
                                   content_type="text/html", body=GENUINE_ABOUT)

        fetcher = OldFetcher()
        out = requalify_record(station(), fetcher=fetcher,
                               fetch_timeout=5.0, record_budget=30.0)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertIn("https://example.org/", fetcher.calls)

    def test_fetch_within_returns_result_on_the_happy_path(self):
        fetcher = FakeFetcher(pages={"https://ok.org/": GENUINE_HOME})
        got = fetch_within(fetcher, "https://ok.org/", wall_timeout=5.0)
        self.assertFalse(got.timed_out)
        self.assertIsNone(got.error)
        self.assertTrue(got.result.ok)

    def test_fetch_within_flags_timeout_without_raising(self):
        fetcher = HangingFetcher(hang_urls={"https://slow.org/"})
        got = fetch_within(fetcher, "https://slow.org/", wall_timeout=0.3)
        self.assertTrue(got.timed_out)
        self.assertIsNone(got.result)
        self.assertLess(got.waited, 5.0)

    def test_abandoned_worker_is_a_daemon_so_exit_is_never_blocked(self):
        """A leaked thread must not keep the process alive after the report."""
        fetcher = HangingFetcher(hang_urls={"https://slow.org/"}, delay=60.0)
        started = time.monotonic()
        out = requalify_record(
            station(station_id="domain:slow.org", domain="slow.org",
                    website="https://slow.org/"),
            fetcher=fetcher, fetch_timeout=0.3, record_budget=1.0)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(out.proposed_kind, "fetch_timeout")
        live = [t for t in threading.enumerate()
                if t.name == "requalify-fetch" and t.is_alive()]
        self.assertTrue(live)
        self.assertTrue(all(t.daemon for t in live),
                        "abandoned fetch threads must be daemons")

    def test_keyboard_interrupt_is_not_swallowed_as_a_data_error(self):
        """An operator must still be able to stop the run."""
        class Interrupting:
            def fetch(self, url, **_kwargs):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            requalify_record(station(), fetcher=Interrupting(),
                             fetch_timeout=2.0, record_budget=5.0)


class FlakyFetcher:
    """Fails the first ``fails`` HOMEPAGE attempts, then serves real pages.

    Models the failure that motivated the retry policy: a transient DNS or
    connection blip on an otherwise healthy station site. Only the homepage
    is made flaky so that call counts reflect the retry, not the later
    sub-page fetches.
    """

    def __init__(self, error_kind, *, fails=1, status=None, body=None):
        self.error_kind = error_kind
        self.fails = fails
        self.status = status
        self.body = body or GENUINE_HOME
        self.calls: list[str] = []
        self.homepage = "https://example.org/"

    def fetch(self, url, **_kwargs):
        self.calls.append(url)
        if url == self.homepage and len(
                [c for c in self.calls if c == self.homepage]) <= self.fails:
            return FetchResult(url=url, status=self.status,
                               error_kind=self.error_kind,
                               error_message=self.error_kind)
        body = self.body if url == self.homepage else GENUINE_ABOUT
        return FetchResult(url=url, final_url=url, status=200,
                           content_type="text/html", body=body)

    @property
    def homepage_calls(self) -> int:
        return len([c for c in self.calls if c == self.homepage])


class AlwaysFailsFetcher:
    def __init__(self, error_kind, status=None):
        self.error_kind = error_kind
        self.status = status
        self.calls: list[str] = []

    def fetch(self, url, **_kwargs):
        self.calls.append(url)
        return FetchResult(url=url, status=self.status,
                           error_kind=self.error_kind,
                           error_message=self.error_kind)


class TransientRetryTests(unittest.TestCase):
    """Only genuine network interruptions are retried, and a network failure
    can never be laundered into a REJECTED station."""

    def _record(self, fetcher, **kw):
        return requalify_record(
            station(), fetcher=fetcher, retry_backoff=0.0,
            fetch_timeout=5.0, record_budget=30.0, **kw)

    def test_dns_error_is_retried_then_succeeds(self):
        fetcher = FlakyFetcher("dns_error", fails=1)
        out = self._record(fetcher)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertEqual(fetcher.homepage_calls, 2, "dns_error was not retried")

    def test_connection_error_is_retried_then_succeeds(self):
        fetcher = FlakyFetcher("connection_error", fails=1)
        out = self._record(fetcher)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertEqual(fetcher.homepage_calls, 2,
                         "connection_error was not retried")

    def test_http_429_is_retried_then_succeeds(self):
        fetcher = FlakyFetcher("http_status", fails=1, status=429)
        out = self._record(fetcher)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertEqual(fetcher.homepage_calls, 2, "HTTP 429 was not retried")

    def test_http_503_is_retried_then_succeeds(self):
        fetcher = FlakyFetcher("http_status", fails=1, status=503)
        out = self._record(fetcher)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertEqual(fetcher.homepage_calls, 2, "HTTP 503 was not retried")

    def test_retry_recovers_from_two_consecutive_failures(self):
        fetcher = FlakyFetcher("dns_error", fails=2)
        out = self._record(fetcher)
        self.assertEqual(out.proposed_verdict, "qualified")
        self.assertEqual(fetcher.homepage_calls, 3,
                         "2 retries = 3 calls expected")

    def test_retry_exhaustion_is_needs_review_and_never_rejected(self):
        for kind, status in (("dns_error", None), ("connection_error", None),
                             ("http_status", 429), ("http_status", 503)):
            with self.subTest(kind=kind, status=status):
                fetcher = AlwaysFailsFetcher(kind, status)
                out = self._record(fetcher)
                self.assertEqual(out.proposed_verdict, "needs_review")
                self.assertNotEqual(out.proposed_verdict, "rejected")
                self.assertEqual(out.determinism, NETWORK_REQUIRED)
                # first attempt + 2 retries, and no sub-page fetch happened
                # because the homepage never succeeded.
                self.assertEqual(len(fetcher.calls), 3)

    def test_robots_disallowed_is_not_retried(self):
        fetcher = AlwaysFailsFetcher("robots_disallowed")
        out = self._record(fetcher)
        self.assertEqual(len(fetcher.calls), 1,
                         "robots.txt is a decision, not an interruption")
        self.assertEqual(out.proposed_verdict, "needs_review")
        self.assertNotEqual(out.proposed_verdict, "rejected")

    def test_ordinary_4xx_is_not_retried(self):
        for status in (400, 401, 403, 404, 410, 451):
            with self.subTest(status=status):
                fetcher = AlwaysFailsFetcher("http_status", status)
                out = self._record(fetcher)
                self.assertEqual(len(fetcher.calls), 1,
                                 f"HTTP {status} must not be retried")
                self.assertEqual(out.proposed_verdict, "needs_review")
                self.assertNotEqual(out.proposed_verdict, "rejected")

    def test_server_error_other_than_503_is_not_retried(self):
        fetcher = AlwaysFailsFetcher("http_status", 500)
        self._record(fetcher)
        self.assertEqual(len(fetcher.calls), 1,
                         "only 503 is in the retry allowlist")

    def test_fetch_timeout_is_not_retried(self):
        fetcher = HangingFetcher(hang_urls={"https://example.org/"}, delay=30.0)
        out = requalify_record(station(), fetcher=fetcher, fetch_timeout=0.4,
                               record_budget=5.0, retry_backoff=0.0)
        self.assertEqual(out.proposed_verdict, "needs_review")
        self.assertEqual(out.proposed_kind, "fetch_timeout")
        self.assertEqual(len(fetcher.calls), 1,
                         "a site-level timeout must not be retried as a verdict")

    def test_deterministic_rejection_is_never_fetched_or_retried(self):
        fetcher = FlakyFetcher("dns_error", fails=5)
        out = requalify_record(
            station(website="https://umn.edu/news-events/real-college-radio",
                    domain="umn.edu"),
            fetcher=fetcher, retry_backoff=0.0)
        self.assertEqual(out.proposed_verdict, "rejected")
        self.assertEqual(fetcher.calls, [], "no fetch should happen at all")

    def test_retry_policy_allowlist_is_explicit(self):
        self.assertTrue(is_transient_network_failure("dns_error", None))
        self.assertTrue(is_transient_network_failure("connection_error", None))
        self.assertTrue(is_transient_network_failure("http_status", 429))
        self.assertTrue(is_transient_network_failure("http_status", 503))
        for kind, status in (("robots_disallowed", None), ("http_status", 404),
                             ("http_status", 403), ("http_status", 400),
                             ("too_large", None), ("content_type", None),
                             ("invalid_url", None), (None, 200)):
            with self.subTest(kind=kind, status=status):
                self.assertFalse(is_transient_network_failure(kind, status))

    def test_fetch_with_retries_respects_zero_retries(self):
        fetcher = FlakyFetcher("dns_error", fails=1)
        got = fetch_with_retries(fetcher, "https://example.org/",
                                 wall_timeout=5.0, retry_attempts=0,
                                 retry_backoff=0.0)
        self.assertFalse(got.result.ok)
        self.assertEqual(len(fetcher.calls), 1)

    def test_backoff_actually_waits(self):
        fetcher = FlakyFetcher("dns_error", fails=1)
        started = time.monotonic()
        fetch_with_retries(fetcher, "https://example.org/", wall_timeout=5.0,
                           retry_backoff=0.5, retry_attempts=2)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertEqual(len(fetcher.calls), 2,
                         "the retry should have succeeded")


class NetworkOutageAbortTests(unittest.TestCase):
    """A dead network must stop the run, not produce a confident report.

    The counted failures are EXACTLY the retry allowlist: dns_error,
    connection_error, HTTP 429/503. Slow sites and robots.txt refusals are
    legitimate per-site outcomes and must never be able to abort a run.
    """

    def _ok_outcome(self, dom):
        return requalify_record(
            station(station_id=f"domain:{dom}", domain=dom,
                    website=f"https://{dom}/"),
            fetcher=FakeFetcher({f"https://{dom}/": GENUINE_HOME,
                                 f"https://{dom}/about": GENUINE_ABOUT}),
            retry_backoff=0.0)

    def _failed_outcome(self, dom, kind, status=None):
        return requalify_record(
            station(station_id=f"domain:{dom}", domain=dom,
                    website=f"https://{dom}/"),
            fetcher=AlwaysFailsFetcher(kind, status),
            fetch_timeout=2.0, record_budget=5.0, retry_backoff=0.0)

    def _timeout_outcome(self, dom):
        return requalify_record(
            station(station_id=f"domain:{dom}", domain=dom,
                    website=f"https://{dom}/"),
            fetcher=HangingFetcher(hang_urls={f"https://{dom}/"}, delay=30.0),
            fetch_timeout=0.3, record_budget=5.0, retry_backoff=0.0)

    def _outcomes(self, kinds, domains=None):
        """Legacy helper: build outcomes from coarse kind labels."""
        out = []
        for i, kind in enumerate(kinds):
            dom = (domains[i] if domains else f"s{i}.org")
            if kind == "ok":
                out.append(self._ok_outcome(dom))
            else:
                out.append(self._failed_outcome(dom, kind))
        return out

    def _feed(self, det, outcomes):
        """Feed outcomes; return the 1-based index that tripped, or None."""
        for i, o in enumerate(outcomes, 1):
            if det.record(o):
                return i
        return None

    def test_detector_aborts_above_50_percent_of_window(self):
        det = OutageDetector(window=20, threshold=0.5)
        outcomes = [self._failed_outcome(f"a{i}.org", "dns_error")
                    for i in range(12)]
        outcomes += [self._ok_outcome(f"b{i}.org") for i in range(8)]
        tripped = self._feed(det, outcomes)
        self.assertIsNotNone(tripped, "detector never aborted on a 60% outage")
        self.assertEqual(tripped, 20, "should abort once the window is full")
        self.assertTrue(det.aborted)
        self.assertGreater(det.failure_rate, det.threshold)

    def test_11_of_20_transient_failures_triggers_the_guard(self):
        det = OutageDetector(window=20, threshold=0.5)
        outcomes = [self._failed_outcome(f"a{i}.org", "dns_error")
                    for i in range(11)]
        outcomes += [self._ok_outcome(f"b{i}.org") for i in range(9)]
        self.assertIsNotNone(self._feed(det, outcomes),
                             "11/20 = 55% must abort")
        self.assertTrue(det.aborted)
        self.assertAlmostEqual(det.failure_rate, 0.55)

    def test_10_of_20_transient_failures_does_not_trigger_the_guard(self):
        det = OutageDetector(window=20, threshold=0.5)
        outcomes = [self._failed_outcome(f"a{i}.org", "dns_error")
                    for i in range(10)]
        outcomes += [self._ok_outcome(f"b{i}.org") for i in range(10)]
        self.assertIsNone(self._feed(det, outcomes),
                          "10/20 = 50% must NOT abort")
        self.assertFalse(det.aborted)

    def test_11_timeouts_plus_9_healthy_does_not_trigger(self):
        """A run of individually slow sites is not a network outage.

        Regression test for the aborted production run: 9 fetch_timeouts
        plus 2 robots_disallowed reached 55% under the old, over-broad
        failure set, needlessly discarding 243 unevaluated records.
        """
        det = OutageDetector(window=20, threshold=0.5)
        outcomes = [self._timeout_outcome(f"slow{i}.org") for i in range(11)]
        outcomes += [self._ok_outcome(f"ok{i}.org") for i in range(9)]
        kinds = {o.proposed_kind for o in outcomes[:11]}
        self.assertEqual(kinds, {"fetch_timeout"},
                         "fixture must actually produce timeouts")
        self.assertIsNone(self._feed(det, outcomes),
                          "timeouts must never abort the run")
        self.assertFalse(det.aborted)
        self.assertEqual(det.failure_rate, 0.0)

    def test_11_robots_disallowed_plus_9_healthy_does_not_trigger(self):
        """robots.txt refusal is a policy decision, never an outage."""
        det = OutageDetector(window=20, threshold=0.5)
        outcomes = [self._failed_outcome(f"r{i}.org", "robots_disallowed")
                    for i in range(11)]
        outcomes += [self._ok_outcome(f"ok{i}.org") for i in range(9)]
        self.assertIsNone(self._feed(det, outcomes),
                          "robots.txt refusals must never abort the run")
        self.assertFalse(det.aborted)
        self.assertEqual(det.failure_rate, 0.0)

    def test_mixed_slow_and_blocked_sites_do_not_trigger(self):
        """The exact shape of the aborted production window: 9 timeouts +
        2 robots blocks = 11/20, which must NOT abort."""
        det = OutageDetector(window=20, threshold=0.5)
        outcomes = [self._timeout_outcome(f"t{i}.org") for i in range(9)]
        outcomes += [self._failed_outcome("nfcb.org", "robots_disallowed"),
                     self._failed_outcome("noncommusic.org",
                                          "robots_disallowed")]
        outcomes += [self._ok_outcome(f"ok{i}.org") for i in range(9)]
        self.assertIsNone(self._feed(det, outcomes),
                          "the production abort scenario must no longer abort")
        self.assertFalse(det.aborted)

    def test_all_four_transient_kinds_trigger_the_guard(self):
        for kind, status in (("dns_error", None),
                             ("connection_error", None),
                             ("http_status", 429), ("http_status", 503)):
            with self.subTest(kind=kind, status=status):
                det = OutageDetector(window=20, threshold=0.5)
                outcomes = [self._failed_outcome(f"a{i}.org", kind, status)
                            for i in range(11)]
                outcomes += [self._ok_outcome(f"b{i}.org") for i in range(9)]
                self.assertIsNotNone(self._feed(det, outcomes),
                                     f"{kind}/{status} must abort at 55%")
                self.assertTrue(det.aborted)

    def test_non_transient_failure_kinds_never_count(self):
        """Everything outside the allowlist is invisible to the guard."""
        for kind, status in (("robots_disallowed", None),
                             ("http_status", 403), ("http_status", 404),
                             ("http_status", 500), ("ssl_error", None),
                             ("content_type", None), ("too_large", None),
                             ("invalid_url", None)):
            with self.subTest(kind=kind, status=status):
                det = OutageDetector(window=20, threshold=0.5)
                outcomes = [self._failed_outcome(f"a{i}.org", kind, status)
                            for i in range(20)]
                self.assertIsNone(self._feed(det, outcomes),
                                  f"{kind} must not count as an outage")
                self.assertFalse(det.aborted)
                self.assertEqual(det.failure_rate, 0.0)

    def test_outage_uses_the_same_predicate_as_retry(self):
        """The two policies must not drift apart again."""
        for kind, status, expected in (
                ("dns_error", None, True),
                ("connection_error", None, True),
                ("http_status", 429, True),
                ("http_status", 503, True),
                ("robots_disallowed", None, False),
                ("http_status", 404, False)):
            with self.subTest(kind=kind, status=status):
                out = requalify_record(
                    station(station_id="domain:x.org", domain="x.org",
                            website="https://x.org/"),
                    fetcher=AlwaysFailsFetcher(kind, status),
                    fetch_timeout=2.0, record_budget=5.0, retry_backoff=0.0)
                self.assertEqual(out.is_transient_network_failure, expected)
                self.assertEqual(
                    classify_transient_outcome(out), expected,
                    "detector and retry policy must agree")

    def test_timeout_outcome_records_a_non_transient_marker(self):
        out = self._timeout_outcome("slow.org")
        self.assertEqual(out.proposed_kind, "fetch_timeout")
        self.assertEqual(out.network_error_kind, "per_site_timeout")
        self.assertFalse(out.is_transient_network_failure)
        self.assertFalse(classify_transient_outcome(out))
        # Still quarantined: a network failure is never a rejection.
        self.assertEqual(out.proposed_verdict, "needs_review")

    def test_exactly_50_percent_does_not_abort(self):
        """The rule is 'more than' the threshold, so 50% is allowed."""
        det = OutageDetector(window=20, threshold=0.5)
        for o in self._outcomes(["dns_error"] * 10 + ["ok"] * 10):
            self.assertFalse(det.record(o))
        self.assertFalse(det.aborted)
        self.assertEqual(det.failure_rate, 0.5)

    def test_below_threshold_run_continues_to_completion(self):
        det = OutageDetector(window=20, threshold=0.5)
        outcomes = self._outcomes(["dns_error"] * 6 + ["ok"] * 14)
        for o in outcomes:
            self.assertFalse(det.record(o))
        self.assertFalse(det.aborted)

    def test_window_is_rolling_so_recovery_stops_the_abort(self):
        det = OutageDetector(window=20, threshold=0.5)
        for o in self._outcomes(["dns_error"] * 8):
            det.record(o)
        # Network recovers: the last 20 are dominated by successes.
        for o in self._outcomes(["ok"] * 15):
            self.assertFalse(det.record(o))
        self.assertFalse(det.aborted)

    def test_partial_window_never_aborts(self):
        det = OutageDetector(window=20, threshold=0.5)
        for o in self._outcomes(["dns_error"] * 19):
            self.assertFalse(det.record(o),
                             "must not judge before the window is full")
        self.assertFalse(det.aborted)

    def test_threshold_is_configurable(self):
        det = OutageDetector(window=4, threshold=0.75)
        for o in self._outcomes(["dns_error"] * 3 + ["ok"]):
            self.assertFalse(det.record(o),
                             "75% threshold should tolerate 3/4 failures")

    def test_deterministic_rejections_do_not_count_as_outage(self):
        """A run that rejects many article paths is healthy, not an outage."""
        det = OutageDetector(window=4, threshold=0.5)
        rejects = [
            requalify_record(
                station(station_id=f"domain:art{i}.org",
                        domain=f"art{i}.org",
                        website=f"https://art{i}.org/news-events/story-{i}"),
                fetcher=None)
            for i in range(4)
        ]
        self.assertTrue(all(r.proposed_verdict == "rejected" for r in rejects))
        for r in rejects:
            self.assertFalse(det.record(r))
        self.assertFalse(det.aborted)

    def test_run_aborts_early_and_marks_report_untrustworthy(self):
        stations = [station(station_id=f"domain:s{i}.org", domain=f"s{i}.org",
                            website=f"https://s{i}.org/")
                    for i in range(40)]
        fetcher = AlwaysFailsFetcher("dns_error")
        det = OutageDetector(window=20, threshold=0.5, records_total=40)
        report = build_report(stations, fetcher=fetcher, mode="live",
                              retry_backoff=0.0, fetch_timeout=2.0,
                              record_budget=5.0, detector=det)
        self.assertTrue(report.aborted)
        self.assertFalse(report.is_trustworthy)
        # Stopped early rather than grinding through all 40.
        self.assertLess(report.inspected, 40)
        self.assertEqual(report.records_total, 40)
        self.assertIn("NETWORK OUTAGE DETECTED", report.abort_reason)

    def test_aborted_report_says_do_not_trust_it(self):
        report = build_report(
            [station(station_id=f"domain:s{i}.org", domain=f"s{i}.org",
                     website=f"https://s{i}.org/") for i in range(25)],
            fetcher=AlwaysFailsFetcher("dns_error"), mode="live",
            retry_backoff=0.0, fetch_timeout=2.0, record_budget=5.0,
            detector=OutageDetector(window=20, threshold=0.5, records_total=25))
        text = report.render()
        self.assertIn("ABORTED", text)
        self.assertIn("DO NOT TRUST THIS REPORT", text)
        self.assertIn(OUTAGE_ABORT_MESSAGE, text)
        self.assertIn("Re-run when connectivity is stable.", text)

    def test_aborted_report_serialises_the_outage_flag(self):
        report = build_report(
            [station(station_id=f"domain:s{i}.org", domain=f"s{i}.org",
                     website=f"https://s{i}.org/") for i in range(25)],
            fetcher=AlwaysFailsFetcher("connection_error"), mode="live",
            retry_backoff=0.0, fetch_timeout=2.0, record_budget=5.0,
            detector=OutageDetector(window=20, threshold=0.5, records_total=25))
        payload = report.to_dict()
        self.assertTrue(payload["aborted"])
        self.assertFalse(payload["trustworthy"])
        self.assertIn("NETWORK OUTAGE DETECTED", payload["abort_reason"])

    def test_healthy_run_is_trustworthy(self):
        stations = [station(station_id=f"domain:g{i}.org", domain=f"g{i}.org",
                            website=f"https://g{i}.org/") for i in range(30)]
        fetcher = FakeFetcher({f"https://g{i}.org/": GENUINE_HOME for i in range(30)})
        for i in range(30):
            fetcher.pages[f"https://g{i}.org/about"] = GENUINE_ABOUT
        report = build_report(stations, fetcher=fetcher, mode="live",
                              retry_backoff=0.0, fetch_timeout=5.0,
                              record_budget=30.0,
                              detector=OutageDetector(window=20, threshold=0.5,
                                                      records_total=30))
        self.assertFalse(report.aborted)
        self.assertTrue(report.is_trustworthy)
        self.assertEqual(report.inspected, 30)
        self.assertNotIn("ABORTED", report.render())

    def test_offline_mode_does_not_abort_on_network_failures(self):
        """No network is used offline, so the guard must not fire."""
        stations = [station(station_id=f"domain:s{i}.org", domain=f"s{i}.org",
                            website=f"https://s{i}.org/") for i in range(30)]
        report = build_report(stations, fetcher=None, mode="offline",
                              detector=OutageDetector(window=20, threshold=0.5,
                                                      records_total=30))
        self.assertFalse(report.aborted)

    def test_run_full_of_slow_sites_completes_and_is_trustworthy(self):
        """End-to-end version of the production regression."""
        stations = [station(station_id=f"domain:t{i}.org", domain=f"t{i}.org",
                            website=f"https://t{i}.org/")
                    for i in range(40)]
        fetcher = HangingFetcher(
            hang_urls={f"https://t{i}.org/" for i in range(40)}, delay=30.0)
        det = OutageDetector(window=20, threshold=0.5, records_total=40)
        report = build_report(stations, fetcher=fetcher, mode="live",
                              fetch_timeout=0.3, record_budget=2.0,
                              retry_backoff=0.0, detector=det)
        self.assertFalse(report.aborted)
        self.assertTrue(report.is_trustworthy)
        self.assertEqual(report.inspected, 40)
        self.assertEqual(report.totals["needs_review"], 40)
        self.assertEqual(report.totals["rejected"], 0)

    def test_run_full_of_robots_blocks_completes_and_is_trustworthy(self):
        stations = [station(station_id=f"domain:r{i}.org", domain=f"r{i}.org",
                            website=f"https://r{i}.org/")
                    for i in range(40)]
        fetcher = AlwaysFailsFetcher("robots_disallowed")
        det = OutageDetector(window=20, threshold=0.5, records_total=40)
        report = build_report(stations, fetcher=fetcher, mode="live",
                              retry_backoff=0.0, fetch_timeout=2.0,
                              record_budget=5.0, detector=det)
        self.assertFalse(report.aborted)
        self.assertTrue(report.is_trustworthy)
        self.assertEqual(report.inspected, 40)
        self.assertEqual(report.totals["rejected"], 0,
                         "robots blocks must not become rejections")

    def test_report_records_underlying_network_error_kind(self):
        report = build_report(
            [station(station_id="domain:dns.org", domain="dns.org",
                     website="https://dns.org/")],
            fetcher=AlwaysFailsFetcher("dns_error"), mode="live",
            retry_backoff=0.0, fetch_timeout=2.0, record_budget=5.0)
        r = report.outcomes[0]
        self.assertEqual(r.network_error_kind, "dns_error")
        self.assertTrue(r.is_transient_network_failure)
        payload = r.to_dict()
        self.assertEqual(payload["network_error_kind"], "dns_error")
        self.assertTrue(payload["is_transient_network_failure"])

    def test_offline_records_carry_no_transport_failure(self):
        report = build_report(
            [station(station_id="domain:o.org", domain="o.org",
                     website="https://o.org/")],
            fetcher=None, mode="offline")
        r = report.outcomes[0]
        self.assertIsNone(r.network_error_kind)
        self.assertFalse(r.is_transient_network_failure)

    def test_detector_defaults_are_unchanged(self):
        self.assertEqual(DEFAULT_OUTAGE_WINDOW, 20)
        self.assertEqual(DEFAULT_OUTAGE_FAILURE_RATIO, 0.5)
        self.assertEqual(DEFAULT_RETRY_ATTEMPTS, 2)

    def test_retry_allowlist_is_unchanged(self):
        """Requirement 5: the retry policy must not have been widened."""
        self.assertEqual(RETRYABLE_ERROR_KINDS,
                         frozenset({"dns_error", "connection_error"}))
        self.assertEqual(RETRYABLE_HTTP_STATUS, frozenset({429, 503}))
        for kind in ("robots_disallowed", "ssl_error", "content_type",
                     "too_large", "invalid_url", "per_site_timeout"):
            self.assertFalse(is_transient_network_failure(kind, None),
                             f"{kind} must not be transient")
        for status in (400, 401, 403, 404, 410, 500, 451):
            self.assertFalse(is_transient_network_failure("http_status",
                                                           status))


class DefaultTimeoutTests(unittest.TestCase):
    """The CLI default must match production discovery (15s) but stay
    overridable."""

    def _cli_defaults(self, argv):
        """Read parsed CLI args without executing the run."""
        import argparse

        class Stop(Exception):
            pass

        captured = {}
        real_parse = argparse.ArgumentParser.parse_args

        def spy(self, args=None, namespace=None):
            captured["ns"] = self.parse_known_args(args, namespace)[0]
            raise Stop

        argparse.ArgumentParser.parse_args = spy
        try:
            with self.assertRaises(Stop):
                requalify_mod.main(argv)
        finally:
            argparse.ArgumentParser.parse_args = real_parse
        return captured["ns"]

    def test_module_default_is_15_seconds(self):
        self.assertEqual(requalify_mod.DEFAULT_FETCH_TIMEOUT_SECONDS, 15.0)

    def test_cli_default_is_15_seconds(self):
        self.assertEqual(self._cli_defaults(["--dry-run"]).fetch_timeout, 15.0)

    def test_explicit_fetch_timeout_overrides_the_default(self):
        self.assertEqual(
            self._cli_defaults(["--dry-run", "--fetch-timeout", "7"]
                              ).fetch_timeout, 7.0)

    def test_production_fetch_timeout_is_untouched(self):
        """We must not have changed the shared engine's 15s default."""
        import discovery.radio.pipeline as pipeline
        import inspect as _inspect
        src = _inspect.getsource(pipeline.RadioDiscoveryEngine.__init__)
        self.assertIn("timeout_seconds=15.0", src)

    def test_default_retries_and_threshold_are_explicit(self):
        self.assertEqual(requalify_mod.DEFAULT_RETRY_ATTEMPTS, 2)
        self.assertEqual(requalify_mod.DEFAULT_RETRY_BACKOFF_SECONDS, 3.0)
        self.assertEqual(requalify_mod.DEFAULT_OUTAGE_WINDOW, 20)
        self.assertEqual(requalify_mod.DEFAULT_OUTAGE_FAILURE_RATIO, 0.5)


class ProgressReportingTests(unittest.TestCase):
    def _outcomes(self, count):
        return [station(station_id=f"domain:{i}.org", domain=f"{i}.org",
                        website=f"https://{i}.org/") for i in range(count)]

    def test_progress_reports_index_total_verdict_and_kind(self):
        stream = io.StringIO()
        progress = make_progress_printer(3, every=1, stream=stream)
        fetcher = HangingFetcher(
            hang_urls={"https://1.org/"},
            pages={"https://0.org/": GENUINE_HOME,
                   "https://0.org/about": GENUINE_ABOUT,
                   "https://2.org/": GENUINE_HOME,
                   "https://2.org/about": GENUINE_ABOUT})
        requalify_all(self._outcomes(3), fetcher=fetcher, fetch_timeout=0.3,
                      record_budget=2.0, on_record=progress)

        lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)
        self.assertIn("[1/3]", lines[0])
        self.assertIn("[3/3]", lines[2])
        # The example shape from the request must be reproducible.
        self.assertRegex(lines[1], r"\[\d+/3\]\s+[\d.]+s\s+\S+")
        self.assertIn("qualified", lines[0])
        self.assertIn("qualified", lines[2])
        self.assertIn("fetch_timeout", lines[1])

    def test_every_n_throttles_output_but_always_prints_the_last(self):
        stream = io.StringIO()
        progress = make_progress_printer(25, every=10, stream=stream)
        for i in range(25):
            progress(requalify_record(
                station(station_id=f"domain:{i}.org"), fetcher=None))
        lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)          # 10, 20, 25
        self.assertIn("[10/25]", lines[0])
        self.assertIn("[25/25]", lines[-1])

    def test_progress_is_flushed_so_a_piped_run_keeps_moving(self):
        class CountingStream(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flushes = 0

            def flush(self):
                self.flushes += 1

        stream = CountingStream()
        progress = make_progress_printer(2, every=1, stream=stream)
        progress(requalify_record(station(), fetcher=None))
        self.assertGreaterEqual(stream.flushes, 1)

    def test_heartbeat_reports_a_slow_but_alive_fetch(self):
        """Distinguish 'slow site' from 'wedged process' for the operator."""
        events = []
        fetcher = HangingFetcher(hang_urls={"https://slow.org/"}, delay=30.0)
        import discovery.radio.requalify as requalify_mod
        original = requalify_mod._HEARTBEAT_EVERY
        requalify_mod._HEARTBEAT_EVERY = 0.05
        try:
            requalify_record(
                station(station_id="domain:slow.org", domain="slow.org",
                        website="https://slow.org/"),
                fetcher=fetcher, fetch_timeout=1.0, record_budget=5.0,
                heartbeat=lambda st, **kw: events.append(kw))
        finally:
            requalify_mod._HEARTBEAT_EVERY = original
        self.assertTrue(events, "no heartbeat was emitted for a slow fetch")
        self.assertGreater(events[0]["waited"], 0.0)
        self.assertIn("url", events[0])


if __name__ == "__main__":
    unittest.main()
