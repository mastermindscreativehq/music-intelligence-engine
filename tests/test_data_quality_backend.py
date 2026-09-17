"""Data-quality regression tests for the backend/data rules.

User-mandated products:

1. A re-enrichment intake that produced ZERO contacts must never erase the
   contacts already stored for a station (partial/empty waves are additive).
2. DJ + outreach listings never surface dev/test fixtures in production —
   reserved-TLD hosts and unmistakable ``test-dj-*``/``e2e`` markers are
   quarantined on the read path (storage is never mutated), exactly like the
   existing station quarantine.
3. DJ discovery (pipeline) never lets non-DJ pages into the table.
4. The API surfaces plain-language DJ classification status and station
   research status (no engineering jargon).

All data lives in temp SQLite files; no network.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from crawler.http import FetchResult
from crawler.urls import normalize_url

from backend.routes import dispatch
from database.service import PersistenceService
from discovery.djs.pipeline import DjsDiscoveryEngine
from discovery.models import DiscoveryRequest
from discovery.providers import StaticListProvider

from djs.service import create_dj, ingest_dj_discovery
from outreach.service import create_outreach

_CLEANUP_PATH = (Path(__file__).resolve().parents[1] / "scripts"
                 / "dj_classification_cleanup.py")
_spec = importlib.util.spec_from_file_location(
    "dj_classification_cleanup", _CLEANUP_PATH)
dj_cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dj_cleanup)


def _station(contacts, **over):
    base = {
        "name": "W Genre Match", "website": "https://genre-match.org",
        "station_type": "community", "genres": ["rock"], "country": "US",
        "city": "Portland", "confidence_score": 0.9, "status": "enriched",
        "contacts": contacts,
        "submission": None,
    }
    base.update(over)
    return base


class TestContactIntakeIsNonDestructive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        self.service.close()

    def test_empty_contact_wave_never_erases_stored_contacts(self):
        contacts = [
            {"name": "Aisha", "role": "music_director",
             "email": "aisha@genre-match.org", "phone": "+1-555-0100",
             "provenance": [{"source_url": "https://genre-match.org/about",
                             "method": "extraction"}]},
            {"name": "Ben", "role": "programming",
             "email": "ben@genre-match.org",
             "provenance": [{"source_url": "https://genre-match.org/team",
                             "method": "extraction"}]},
        ]
        self.service.ingest_intelligence(
            [_station(contacts)], source="phase10")
        key = self.service.list_stations(limit=1)[0][0]["identity_key"]
        self.assertEqual(len(self.service.get_station_contacts(key)), 2)

        # A re-enrich that found nothing must not wipe the stored facts.
        self.service.ingest_intelligence(
            [_station([], name="W Genre Match",
                      website="https://genre-match.org")], source="phase10")
        remaining = self.service.get_station_contacts(key)
        self.assertEqual(len(remaining), 2)
        emails = {c["email"] for c in remaining}
        self.assertEqual(emails, {"aisha@genre-match.org",
                                  "ben@genre-match.org"})

    def test_nonempty_refresh_still_removes_stale_rows(self):
        contacts = [
            {"name": "Aisha", "role": "music_director",
             "email": "aisha@genre-match.org"},
            {"name": "Old", "role": "dj", "email": "old@genre-match.org"},
        ]
        self.service.ingest_intelligence([_station(contacts)], source="t")
        key = self.service.list_stations(limit=1)[0][0]["identity_key"]
        refresh = [{"name": "Aisha", "role": "music_director",
                    "email": "aisha@genre-match.org"},
                   {"name": "New", "role": "marketing",
                    "email": "new@genre-match.org"}]
        self.service.ingest_intelligence(
            [_station(refresh)], source="t")
        remaining = self.service.get_station_contacts(key)
        emails = {c["email"] for c in remaining}
        self.assertEqual(emails, {"aisha@genre-match.org",
                                  "new@genre-match.org"})


class TestDjAndOutreachQuarantine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))
        self.real = create_dj(self.service, payload={
            "name": "DJ Amara", "city": "Lagos",
            "source_urls": ["https://djamara.fm/"],
            "channels": [
                {"channel": "email", "value": "book@djamara.fm",
                 "source_url": "https://djamara.fm/"},
            ],
        })

    def tearDown(self):
        self.service.close()

    def test_dj_fixture_by_reserved_tld_is_hidden_from_api(self):
        create_dj(self.service, payload={
            "name": "DJ Test", "city": "Accra",
            "source_urls": ["https://test-dj-e2e.example/"],
            "channels": [
                {"channel": "email", "value": "test-dj-e2e@example.test",
                 "source_url": "https://test-dj-e2e.example/"},
            ],
        })
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 1)          # only the real DJ
        self.assertEqual(body["data"]["djs"][0]["dj_id"], self.real["dj_id"])
        self.assertEqual(body["data"]["dev_fixtures_excluded"], 1)

    def test_dj_fixture_by_name_marker_is_hidden_from_api(self):
        create_dj(self.service, payload={
            "name": "test-dj-e2e fixture", "city": "Nairobi",
            "source_urls": ["https://fixture-fm.fm/"],
        })
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["dev_fixtures_excluded"], 1)

    def test_dj_list_still_shows_real_profiles(self):
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["dev_fixtures_excluded"], 0)
        self.assertEqual(body["data"]["djs"][0]["name"], "DJ Amara")

    def test_outreach_fixture_is_hidden_from_api(self):
        create_outreach(self.service, payload={
            "recipient": {"contact_uid": "cu_e2e",
                          "name": "test-dj-e2e",
                          "organization": "Test Corps",
                          "email": "test-dj-e2e@example.test"},
            "subject": "E2E test outreach from prod verify",
            "message": "test message",
        })
        _code, body = dispatch(self.service, "GET", "/api/v1/outreach", {})
        self.assertEqual(body["data"]["total"], 0)
        self.assertEqual(body["data"]["dev_fixtures_excluded"], 1)

    def test_outreach_list_still_shows_real_records(self):
        create_outreach(self.service, payload={
            "recipient": {"contact_uid": "cu_wfmu",
                          "name": "Jessica Romoff",
                          "organization": "WFMU",
                          "email": "jessica@wfmu.org"},
            "subject": "New music for WFMU",
            "message": "Hi Jessica, please consider our track.",
        })
        _code, body = dispatch(self.service, "GET", "/api/v1/outreach", {})
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["dev_fixtures_excluded"], 0)
        self.assertEqual(body["data"]["outreach"][0]["subject"],
                         "New music for WFMU")


class TestDjClassificationStatusInApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        self.service.close()

    def test_verified_classification_is_plain_language(self):
        create_dj(self.service, payload={
            "name": "DJ Amara", "city": "Lagos",
            "source_urls": ["https://djamara.fm/"],
            "verification": {
                "classification": {
                    "verdict": "qualified", "kind": "personal_site",
                    "reason": "title names the DJ",
                    "evidence_url": "https://djamara.fm/",
                    "evaluated_at": "2026-09-17T00:00:00Z",
                },
            },
        })
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["djs"][0]["classification_status"],
                         "verified")

    def test_unverified_legacy_record_needs_verification(self):
        create_dj(self.service, payload={
            "name": "DJ Marta", "city": "Nairobi",
            "source_urls": ["https://marta.fm/"],
        })
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["djs"][0]["classification_status"],
                         "needs_verification")

    def test_detail_carries_classification_evidence(self):
        dj = create_dj(self.service, payload={
            "name": "DJ Amara", "city": "Lagos",
            "source_urls": ["https://djamara.fm/"],
            "verification": {
                "classification": {
                    "verdict": "qualified", "kind": "personal_site",
                    "reason": "title names the DJ",
                    "evidence_url": "https://djamara.fm/",
                },
            },
        })
        _code, body = dispatch(self.service, "GET",
                               f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(body["data"]["classification"]["verdict"],
                         "qualified")
        self.assertEqual(body["data"]["classification"]["evidence_url"],
                         "https://djamara.fm/")


class TestStationResearchStatusInApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        self.service.close()

    def test_station_summary_exposes_research_status(self):
        self.service.ingest_intelligence([{
            "name": "W Genre Match", "website": "https://genre-match.org",
            "station_type": "community", "genres": ["rock"],
            "country": "US", "city": "Portland",
            "description": "Community radio for Portland.",
            "confidence_score": 0.5, "status": "enriched",
            "submission": None,
        }], source="t")
        _code, body = dispatch(self.service, "GET", "/api/v1/stations", {})
        self.assertEqual(body["data"]["stations"][0]["research_status"],
                         "partially_researched")

        self.service.ingest_intelligence([{
            "name": "Mystery Signal", "website": "https://mystery-fm.fm",
            "station_type": "unknown", "confidence_score": None,
            "status": "new", "submission": None,
        }], source="t")
        _code, body = dispatch(self.service, "GET", "/api/v1/stations", {})
        statuses = {s["name"]: s["research_status"]
                    for s in body["data"]["stations"]}
        self.assertEqual(statuses["Mystery Signal"], "needs_research")


class _FakeFetcher:
    """URL-keyed fetcher double with a dns_error for missing pages."""

    def __init__(self, pages):
        self.pages = {normalize_url(url): entry
                      for url, entry in pages.items()}

    def fetch(self, url: str) -> FetchResult:
        try:
            key = normalize_url(url)
        except ValueError:
            key = url
        entry = self.pages.get(key)
        result = FetchResult(url=url)
        if entry is None:
            result.error_kind = "dns_error"
            result.error_message = "fixture miss"
            return result
        result.status = entry.get("status", 200)
        result.content_type = entry.get("content_type", "text/html")
        result.body = entry.get("body", "")
        result.final_url = url
        return result


def _page(title: str) -> str:
    return (f"<!doctype html><html><head><title>{title}</title></head>"
            f"<body><h1>{title}</h1><p>Fixture body.</p></body></html>")


class TestPipelineQualificationGate(unittest.TestCase):
    def test_non_dj_pages_never_reach_records(self):
        fetcher = _FakeFetcher({
            "https://amara.example": {"body": _page("DJ Amara | Official Site")},
            "https://zulu.example/sets": {"body": _page("DJ Zulu | Mixes")},
            "https://generic.example": {
                "body": _page("Afrobeats To The World")},
            "https://eventbrite.example": {
                "body": _page("AFROBEATS | Eventbrite")},
        })
        provider = StaticListProvider([
            {"name": "DJ Amara", "url": "https://amara.example"},
            {"name": "DJ Zulu", "url": "https://zulu.example/sets"},
            {"name": "Afrobeats To The World",
             "url": "https://generic.example"},
            {"name": "AFROBEATS | Eventbrite",
             "url": "https://eventbrite.example"},
        ])
        engine = DjsDiscoveryEngine(provider, fetcher=fetcher)
        result = engine.run(DiscoveryRequest(query="DJ", limit=10))

        names = {r["name"] for r in result.records}
        self.assertEqual(names, {"DJ Amara"})   # Zulu page is a mix page
        kinds = {f.error_kind for f in result.failures}
        self.assertIn("not_a_dj", kinds)
        self.assertIn("needs_human_review", kinds)

    def test_qualified_candidates_carry_classification_evidence(self):
        fetcher = _FakeFetcher({
            "https://amara.example": {"body": _page("DJ Amara | Official Site")},
        })
        engine = DjsDiscoveryEngine(
            StaticListProvider(
                [{"name": "DJ Amara", "url": "https://amara.example"}]),
            fetcher=fetcher)
        result = engine.run(DiscoveryRequest(query="DJ", limit=5))
        self.assertEqual(len(result.records), 1)
        classification = result.records[0]["verification"]["classification"]
        self.assertEqual(classification["verdict"], "qualified")
        self.assertEqual(classification["evidence_url"],
                         "https://amara.example")


class TestDjRejectedExclusion(unittest.TestCase):
    """Rejected (NOT a DJ) records never surface in the normal listing."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))
        self.real = create_dj(self.service, payload={
            "name": "DJ Amara", "city": "Lagos",
            "source_urls": ["https://djamara.fm/"],
        })
        self.rejected = create_dj(self.service, payload={
            "name": "Yelp DJ search", "city": "Austin",
            "source_urls": ["https://www.yelp.com/search?find_desc=djs"],
            "verification": {
                "classification": {
                    "verdict": "rejected", "kind": "non_dj",
                    "reason": "not a DJ profile",
                    "evidence_url": "https://www.yelp.com/search?find_desc=djs",
                    "evaluated_at": "2026-09-17T00:00:00Z",
                },
            },
        })

    def tearDown(self):
        self.service.close()

    def test_rejected_row_is_hidden_and_counted(self):
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["rejected_excluded"], 1)
        self.assertEqual(body["data"]["djs"][0]["dj_id"], self.real["dj_id"])

    def test_rejected_detail_is_still_reachable_and_plain_language(self):
        _code, body = dispatch(
            self.service, "GET",
            f"/api/v1/djs/{self.rejected['dj_id']}", {})
        self.assertEqual(body["data"]["classification_status"],
                         "not_qualified")

    def test_needs_verification_rows_stay_visible(self):
        create_dj(self.service, payload={
            "name": "DJ Marta", "city": "Nairobi",
            "source_urls": ["https://marta.fm/"],
            "verification": {
                "classification": {
                    "verdict": "needs_verification", "kind": None,
                    "reason": "no evidence yet",
                    "evidence_url": "https://marta.fm/",
                    "evaluated_at": "2026-09-17T00:00:00Z",
                },
            },
        })
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(body["data"]["rejected_excluded"], 1)


class TestDjIngestRejectGuard(unittest.TestCase):
    """R4: a rejected source URL must never re-introduce a DJ record."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))
        self.bad_url = "https://www.eventbrite.com/e/afrobeats-night-123"
        create_dj(self.service, payload={
            "name": "AFROBEATS | Eventbrite", "city": "Austin",
            "source_urls": [self.bad_url],
            "verification": {
                "classification": {
                    "verdict": "rejected", "kind": "non_dj",
                    "reason": "not a DJ profile",
                    "evidence_url": self.bad_url,
                    "evaluated_at": "2026-09-17T00:00:00Z",
                },
            },
        })

    def tearDown(self):
        self.service.close()

    def test_reintroduced_source_is_refused(self):
        report = ingest_dj_discovery(self.service, [{
            "name": "Afrobeats Night Austin", "city": "Austin",
            "source_urls": [self.bad_url],
        }])
        self.assertEqual(report["created"], 0)
        self.assertEqual(len(report["failures"]), 1)
        self.assertIn("already classified not a DJ",
                      report["failures"][0]["error"])
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 0)
        self.assertEqual(body["data"]["rejected_excluded"], 1)


class TestStationEnrichmentIsNullOnly(unittest.TestCase):
    """Re-enrichment fills empty columns and never overwrites stored facts."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        self.service.close()

    def _ingest(self, **over):
        record = {
            "name": "W Genre Match", "website": "https://genre-match.org",
            "station_type": "community", "status": "enriched",
            "submission": None,
        }
        record.update(over)
        self.service.ingest_intelligence([record], source="t")

    def test_existing_values_survive_and_nulls_are_filled(self):
        self._ingest(description="First description", city="Portland")
        self._ingest(description="Second description", city="Seattle",
                     state_or_region="OR", genres=["rock"])
        row = self.service.list_stations(limit=1)[0][0]
        self.assertEqual(row["description"], "First description")
        self.assertEqual(row["city"], "Portland")
        self.assertEqual(row["state_or_region"], "OR")
        self.assertIn("rock", row["genres"])


class TestDjClassificationCleanup(unittest.TestCase):
    """The cleanup script classifies stored rows idempotently and reversibly."""

    ROWS = (
        ("AFROBEATS | Eventbrite",
         "https://www.eventbrite.com/e/afrobeats-night-123", "rejected"),
        ("DJ Holger", "https://ra.co/events/2398661", "rejected"),
        ("RED CARPET: Kenya's DJ Fully Focus",
         "https://www.youtube.com/watch?v=abc123", "needs_verification"),
        ("DJ Kenz | NY & NJ DJ",
         "https://www.instagram.com/djkenz_/", "needs_verification"),
        ("Grand Performances",
         "https://www.grandperformances.org/events/summer", "rejected"),
        ("DJ Hol Up", "https://open.spotify.com/playlist/37i9dQZF1DX",
         "rejected"),
        ("Best DJs Near Me",
         "https://www.yelp.com/search?find_desc=djs", "rejected"),
        ("Afrobeats Scene Grows",
         "https://blog.eventnoire.com/nyc-afrobeats-2026/", "rejected"),
        ("Afrobeats To The World", "https://afrobeatstotheworld.com/",
         "needs_verification"),
        ("DJ Talk Group",
         "https://www.facebook.com/groups/djtalk/posts/123", "rejected"),
    )

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))
        for name, url, _verdict in self.ROWS:
            create_dj(self.service, payload={
                "name": name, "source_urls": [url]})

    def tearDown(self):
        self.service.close()

    def test_plan_apply_replan_and_reverse(self):
        now = "2026-09-17T00:00:00Z"
        plan = dj_cleanup.plan_cleanup(self.service, now=now)
        verdicts = sorted(
            (c["name"], c["target"]["verdict"]) for c in plan)
        expected = sorted((name, verdict) for name, _u, verdict in self.ROWS)
        self.assertEqual(verdicts, expected)

        rejected = [c for c in plan if c["target"]["verdict"] == "rejected"]
        review = [c for c in plan if c["target"]["verdict"] != "rejected"]
        self.assertEqual(len(rejected), 7)
        self.assertEqual(len(review), 3)
        for change in rejected:
            self.assertEqual(change["target"]["kind"], "non_dj")

        applied = dj_cleanup.apply_cleanup(self.service, plan)
        self.assertEqual(applied, 10)
        self.assertEqual(dj_cleanup.plan_cleanup(self.service, now=now), [])
        self.assertEqual(dj_cleanup.apply_cleanup(self.service, []), 0)

        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 3)
        self.assertEqual(body["data"]["rejected_excluded"], 7)

        # Reversal restores exactly the pre-cleanup listing.
        reverted = dj_cleanup.reverse_cleanup(self.service)
        self.assertEqual(reverted, 10)
        self.assertEqual(dj_cleanup.reverse_cleanup(self.service), 0)
        _code, body = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 10)
        self.assertEqual(body["data"]["rejected_excluded"], 0)


if __name__ == "__main__":
    unittest.main()