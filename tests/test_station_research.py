"""Targeted tests for generalized station deep-research automation.

Everything here runs against injected in-memory fetchers and local HTML
fixtures — NO real SerpAPI request, NO real API key, NO network. Every
SerpAPI test is explicitly mocked (requirement: mock the HTTP response;
never require a real key).

Covered:

- SerpAPI station provider (mock): wire format, provenance, unconfigured,
  key never leaked.
- Deterministic candidate qualification: rejects non-station pages, keeps
  station signals and ambiguous candidates, and is NOT station-specific.
- ``stations.service.ingest_station_discovery``: idempotent, additive,
  validation failures reported (never silently dropped).
- ``discovery.radio.jobs.run_radio_discovery_job`` + the generic dispatcher:
  end-to-end discovery -> enrichment -> single persistence path, honest
  counters, unconfigured provider, org-type aliases, run ledger.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crawler.http import FetchResult
from crawler.urls import normalize_url

from discovery.djs.http_provider import DiscoveryProviderNotConfigured
from discovery.djs.serpapi_provider import SERPAPI_API_KEY_ENV
from discovery.jobs import run_discovery_job
from discovery.models import DiscoveryRequest
from discovery.radio.jobs import run_radio_discovery_job
from discovery.radio.qualify import (
    NEEDS_REVIEW,
    QUALIFIED,
    REJECTED,
    classify_station_candidate,
)
from discovery.radio.serpapi_provider import SerpApiSearchRadioProvider
from discovery.radio.selector import select_radio_discovery_provider

from database.service import PersistenceService
from stations.service import ingest_station_discovery

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGES = FIXTURES / "pages"


def _load(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


SERPAPI_KEY = "TEST-STATION-KEY"
SERPAPI_PAYLOAD = {
    "organic_results": [
        {"position": 1, "title": "KQXR 101.5 FM — Community Radio",
         "link": "https://kqxr.example/",
         "snippet": "Listener-supported community radio in Cedar Valley."},
        {"position": 2, "title": "KQXR playlist",
         "link": "https://open.spotify.com/playlist/kqxr"},
        {"position": 3, "title": "KQXR concert tickets",
         "link": "https://tickets.example/event/kqxr"},
        {"position": 4, "title": "Local radio news roundup",
         "link": "https://news.example/article/kqxr-roundup"},
    ]
}


class _Fetch:
    """Minimal FetchResult double exposing the ``.ok`` / ``.body`` seam."""

    def __init__(self, body=None, *, ok=True):
        self.ok = ok
        self.body = body
        self.status = 200 if ok else None
        self.content_type = "application/json"
        self.error_kind = None
        self.error_message = None
        self.final_url = None

    def __getattr__(self, name):
        raise AttributeError(name)


class _Web:
    """URL-keyed fake web: SerpAPI JSON + station HTML, unified fetcher."""

    def __init__(self, *, payload=None, pages=None):
        self.payload = payload if payload is not None else SERPAPI_PAYLOAD
        self.pages = {normalize_url(u): b for u, b in (pages or {}).items()}
        self.calls: list[str] = []

    def fetch(self, url, **kwargs):
        self.calls.append(url)
        if "serpapi.com/search" in url or "/search?" in url:
            body = self.pages.get(url)
            if body is None:
                body = json.dumps(self.payload)
            return FetchResult(
                url=url, status=200, content_type="application/json",
                body=body, final_url=url)
        try:
            key = normalize_url(url)
        except ValueError:
            key = url
        body = self.pages.get(key)
        if body is None:
            return FetchResult(url=url, error_kind="dns_error",
                               error_message="fixture miss")
        return FetchResult(url=url, status=200, content_type="text/html",
                           body=body, final_url=url)


def _station_pages() -> dict:
    return {
        "https://kqxr.example/": _load("kqxr_home.html"),
        "https://kqxr.example/contact": _load("kqxr_contact.html"),
        "https://kqxr.example/submissions": _load("kqxr_submissions.html"),
    }


def _request(**overrides) -> DiscoveryRequest:
    fields = {"query": "community radio", "limit": 25}
    fields.update(overrides)
    return DiscoveryRequest(**fields)


class SerpApiStationProviderTests(unittest.TestCase):
    """Mocked SerpAPI station provider — no real key, no network."""

    def _provider(self, *, fetcher=None, api_key=SERPAPI_KEY, base_url=None):
        return SerpApiSearchRadioProvider(
            base_url=base_url if base_url is not None
            else "https://serpapi.com/search",
            api_key=api_key, fetcher=fetcher or _Web())

    def test_configured_true_with_key_and_base(self):
        self.assertTrue(self._provider().configured)

    def test_configured_false_without_key(self):
        self.assertFalse(self._provider(api_key="").configured)

    def test_search_raises_when_not_configured(self):
        with self.assertRaises(DiscoveryProviderNotConfigured):
            self._provider(api_key="").search(_request(), ["community radio"])

    def test_maps_organic_results_with_honest_provenance(self):
        out = self._provider().search(_request(), ["community radio"])
        urls = [c.url for c in out]
        self.assertIn("https://kqxr.example/", urls)
        first = next(c for c in out if c.url == "https://kqxr.example/")
        self.assertEqual(first.source, "serpapi_google")
        self.assertIn("Cedar Valley", first.snippet)

    def test_api_key_never_in_candidates(self):
        out = self._provider().search(_request(), ["community radio"])
        for candidate in out:
            for value in candidate.to_dict().values():
                if isinstance(value, str):
                    self.assertNotIn(SERPAPI_KEY, value)

    def test_selector_returns_unconfigured_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = select_radio_discovery_provider(fetcher=_Web())
        self.assertFalse(provider.configured)

    def test_selector_returns_serpapi_when_env_set(self):
        env = {SERPAPI_API_KEY_ENV: SERPAPI_KEY}
        with mock.patch.dict(os.environ, env, clear=True):
            provider = select_radio_discovery_provider(fetcher=_Web())
        self.assertTrue(provider.configured)
        self.assertIsInstance(provider, SerpApiSearchRadioProvider)


class StationQualificationTests(unittest.TestCase):
    """Deterministic, generic (not station-specific) candidate rules."""

    def test_platform_host_is_rejected(self):
        v = classify_station_candidate(
            url="https://facebook.com/wfmu", title="WFMU")
        self.assertEqual(v.verdict, REJECTED)
        self.assertEqual(v.kind, "platform_page")

    def test_streaming_host_is_rejected(self):
        v = classify_station_candidate(
            url="https://open.spotify.com/playlist/x", title="Some station")
        self.assertEqual(v.verdict, REJECTED)

    def test_article_path_is_rejected(self):
        v = classify_station_candidate(
            url="https://news.example/article/local-radio",
            title="Local radio news")
        self.assertEqual(v.verdict, REJECTED)
        self.assertEqual(v.kind, "non_station_path")

    def test_ticket_title_is_rejected(self):
        v = classify_station_candidate(
            url="https://eventportal.example/", title="Buy concert tickets now")
        self.assertEqual(v.verdict, REJECTED)
        self.assertEqual(v.kind, "non_station_title")

    def test_station_signal_rescues_suspicious_title(self):
        v = classify_station_candidate(
            url="https://kqxr.example/", title="KQXR Radio Playlists")
        self.assertEqual(v.verdict, QUALIFIED)

    def test_station_signals_are_qualified(self):
        for url, title in (
            ("https://kqxr.example/", "KQXR 101.5 FM"),
            ("https://wxyz.example/", "WXYZ"),
            ("https://wldg.example/", "WLDG Freeform Radio"),
            ("https://some-unfamiliar-station.example/",
             "Some Unfamiliar Station 91.7 FM"),
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    classify_station_candidate(url=url, title=title).verdict,
                    QUALIFIED)

    def test_ambiguous_is_kept_for_processing(self):
        v = classify_station_candidate(
            url="https://unknown.example/", title="S0")
        self.assertEqual(v.verdict, NEEDS_REVIEW)

    def test_rules_are_not_station_specific(self):
        # A brand-new station qualifies on generic signals; a known station's
        # social page is rejected purely because it is a platform page.
        self.assertEqual(
            classify_station_candidate(
                url="https://brand-new.example/",
                title="Brand New Community Radio").verdict,
            QUALIFIED)
        self.assertEqual(
            classify_station_candidate(
                url="https://instagram.com/known_station").verdict,
            REJECTED)


class StationIngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = PersistenceService(
            os.path.join(self._tmp.name, "mie.db"))

    def tearDown(self):
        self.repo.close()
        self._tmp.cleanup()

    @staticmethod
    def _record(**overrides) -> dict:
        record = {
            "organization_type": "radio_station",
            "name": "Test FM",
            "website": "https://testfm.example",
            "country": "United States",
            "source_urls": ["https://testfm.example/"],
            "emails": [{
                "value": "info@testfm.example",
                "source_url": "https://testfm.example/contact",
                "source_type": "contact_page",
                "method": "text_rule",
                "discovered_at": "2026-01-01T00:00:00+00:00",
                "also_seen_at": [],
            }],
            "contacts": [],
        }
        record.update(overrides)
        return record

    def test_ingest_is_idempotent(self):
        first = ingest_station_discovery(self.repo, [self._record()])
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["merged"], 0)
        second = ingest_station_discovery(self.repo, [self._record()])
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["merged"], 1)
        rows, total = self.repo.list_stations(limit=50)
        self.assertEqual(total, 1)

    def test_ingest_is_additive(self):
        ingest_station_discovery(self.repo, [self._record()])
        extra = self._record()
        extra["emails"] = [{
            "value": "music@testfm.example",
            "source_url": "https://testfm.example/submissions",
            "source_type": "submission_page",
            "method": "text_rule",
            "discovered_at": "2026-01-02T00:00:00+00:00",
            "also_seen_at": [],
        }]
        ingest_station_discovery(self.repo, [extra])
        emails = self.repo.get_station_emails("domain:testfm.example")
        values = {e["value"] for e in emails}
        self.assertEqual(values, {"info@testfm.example",
                                  "music@testfm.example"})

    def test_invalid_record_reported_not_dropped(self):
        bad = self._record(organization_type="venue", name="Not A Station")
        result = ingest_station_discovery(self.repo, [bad])
        self.assertEqual(result["records_accepted"], 0)
        self.assertTrue(result["failures"])


class StationJobTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = PersistenceService(
            os.path.join(self._tmp.name, "mie.db"))
        self.web = _Web(pages=_station_pages())
        self._env = mock.patch.dict(
            os.environ, {SERPAPI_API_KEY_ENV: SERPAPI_KEY}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.repo.close()
        self._tmp.cleanup()

    def test_end_to_end_run_ingests_station_and_rejects_junk(self):
        report = run_radio_discovery_job(
            self.repo, _request().to_dict(), fetcher=self.web)
        self.assertEqual(report["provider"], "serpapi_google")
        self.assertEqual(report["candidates_found"], 1)
        self.assertGreaterEqual(report["records_ingested"], 1)
        station = self.repo.get_station("domain:kqxr.example")
        self.assertIsNotNone(station)
        self.assertTrue(station["name"])
        # Junk candidates were never fetched.
        self.assertFalse(any("spotify" in u for u in self.web.calls))
        self.assertFalse(any("tickets.example" in u for u in self.web.calls))

    def test_second_run_merges_without_duplicating(self):
        run_radio_discovery_job(
            self.repo, _request().to_dict(), fetcher=self.web)
        report = run_radio_discovery_job(
            self.repo, _request().to_dict(), fetcher=self.web)
        self.assertGreaterEqual(report["duplicates"], 1)
        rows, total = self.repo.list_stations(limit=50)
        self.assertEqual(total, 1)

    def test_unconfigured_provider_is_honest(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DiscoveryProviderNotConfigured):
                run_radio_discovery_job(
                    self.repo, _request().to_dict(),
                    fetcher=_Web(pages=_station_pages()))

    def test_dispatcher_accepts_radio_and_dispatches_station_pipeline(self):
        # Proves the POST /api/v1/discovery/jobs handler path (it calls exactly
        # ``discovery.jobs.run_discovery_job``): organization_type=radio (and
        # its canonical alias) is accepted, routed to the *station* deep-
        # research runner, and NEVER downgraded into the DJ runner.
        import discovery.jobs as discovery_jobs_mod
        dj_calls: list[str] = []
        radio_calls: list[dict] = []

        def _spy_dj(*_args, **_kwargs):
            dj_calls.append("dj")
            raise AssertionError(
                "radio jobs must never be routed to the DJ runner")

        def _spy_radio(repository, config, *, fetcher=None):
            radio_calls.append(config)
            return run_radio_discovery_job(repository, config,
                                           fetcher=fetcher)

        with mock.patch.dict(
                discovery_jobs_mod._RUNNERS,
                {"dj": _spy_dj, "radio": _spy_radio, "station": _spy_radio},
                clear=True):
            for org_type in ("radio", "station"):
                with self.subTest(org_type=org_type):
                    report = run_discovery_job(
                        self.repo,
                        {"organization_type": org_type,
                         "query": "community radio", "limit": 5},
                        fetcher=self.web)
                    self.assertIn(
                        report["status"],
                        ("completed", "completed_with_failures"))
                    self.assertGreaterEqual(report["records_ingested"], 1)
                    stored = self.repo.get_discovery_job(report["run_id"])
                    self.assertIsNotNone(stored)
                    self.assertEqual(stored["organization_type"], org_type)

        self.assertEqual(dj_calls, [],
                         "radio jobs must dispatch the station runner")
        self.assertEqual(len(radio_calls), 2)

    def test_dispatcher_rejects_unsupported_org_type(self):
        with self.assertRaises(ValueError):
            run_discovery_job(
                self.repo, {"organization_type": "venue", "query": "x"},
                fetcher=self.web)

    def test_not_configured_run_is_recorded_in_ledger(self):
        recorded: list[dict] = []
        original = self.repo.record_discovery_job

        def _spy(report):
            recorded.append(dict(report))
            return original(report)

        self.repo.record_discovery_job = _spy
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DiscoveryProviderNotConfigured):
                run_discovery_job(
                    self.repo,
                    {"organization_type": "radio", "query": "community radio"},
                    fetcher=_Web(pages=_station_pages()))
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["status"], "not_configured")
        self.assertEqual(recorded[0]["organization_type"], "radio")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
