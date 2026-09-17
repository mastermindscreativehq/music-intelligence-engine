"""Phase 4b: DJ discovery pipeline — independence, provenance, dedup.

Verifies the user-mandated contract for the corrected DJ feature:

1. A DJ record is valid WITHOUT any station affiliation (station_key and
   station_name are optional metadata, never a requirement).
2. A DJ MAY carry an optional station affiliation without deriving from it.
3. Station ingestion (radio pipeline) NEVER creates DJ records.
4. Search/listing returns INDEPENDENT DJs and filters by dj_type, platform,
   and contact availability.
5. Discovery channels always retain their http(s) ``source_url`` provenance.
6. Repeated discovery of the same DJ merges (dedup) instead of duplicating.
7. No fabricated contacts: an unreachable DJ site produces a record with
   zero channels and an empty contact state.

All data lives in temp dirs / in-memory fakes; no real DB or network.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from crawler.http import FetchResult
from crawler.urls import normalize_url

from backend.routes import dispatch
from database.service import PersistenceService
from discovery.djs.pipeline import (
    DjsDiscoveryEngine,
    build_dj_queries,
    load_dj_seed_entries,
)
from discovery.models import Candidate, DiscoveryRequest
from discovery.providers import StaticListProvider
from djs.service import (
    create_dj,
    dj_detail,
    ingest_dj_discovery,
    list_djs,
)

VALID_DJ = {
    "name": "DJ Amara",
    "role": "club_dj",
    "country": "NG",
    "city": "Lagos",
    "genres": ["afrobeats"],
    "source_urls": ["https://profile.example/amara"],
    "channels": [
        {"channel": "email", "value": "book@amara.example",
         "source_url": "https://profile.example/amara"},
        {"channel": "instagram", "value": "@dj_amara",
         "source_url": "https://profile.example/amara"},
    ],
}


class _FakeFetcher:
    """URL-keyed fetcher double (mirrors test_phase2_station_contract)."""

    def __init__(self, pages: dict[str, dict]):
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


GOOD_PAGE = """<!doctype html><html><head><title>DJ Amara | Official Site</title>
</head><body><h1>DJ Amara</h1>
<p>Bookings and bookings@amara.example. Follow @dj_amara.</p>
<a href="https://amara.example/contact">Contact</a>
<a href="https://amara.example/booking">Bookings</a>
<a href="https://instagram.com/dj_amara">Instagram</a>
</body></html>"""

SILENT_PAGE = """<!doctype html><html><head><title>DJ Sontu</title></head>
<body><h1>DJ Sontu</h1><p>Under construction.</p></body></html>"""


def _seed_file(entries: list[dict]) -> str:
    path = os.path.join(tempfile.mkdtemp(), "seed.json")
    Path(path).write_text(json.dumps({"djs": entries}), encoding="utf-8")
    return path


class TestDJQueries(unittest.TestCase):
    def test_geo_and_genre_queries(self):
        req = DiscoveryRequest(query="DJ", city="Lagos", genre="afrobeats")
        queries = build_dj_queries(req)
        self.assertIn("DJ afrobeats Lagos", queries)
        self.assertTrue(any("club" in q for q in queries))
        self.assertTrue(any("bookings" in q for q in queries))

    def test_no_geo_queries(self):
        req = DiscoveryRequest(query="DJ")
        queries = build_dj_queries(req)
        self.assertEqual(queries[0], "DJ")
        self.assertTrue(all("DJ" in q for q in queries))


class TestDiscoveryNoStation(unittest.TestCase):
    """TESTS 1-2: independent DJ records carry no required station."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        self.service.close()

    def test_dj_created_without_any_station(self):
        payload = {k: v for k, v in VALID_DJ.items()}
        payload.pop("station_key", None)
        payload.pop("station_name", None)
        dj = create_dj(self.service, payload=payload)
        self.assertIsNone(dj["station_key"])
        self.assertIsNone(dj["station_name"])
        rows, total = list_djs(self.service, limit=100)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["dj_id"], dj["dj_id"])

    def test_optional_affiliation_is_metadata_only(self):
        payload = dict(VALID_DJ)
        payload["station_key"] = "domain:neighborhood.example"
        payload["station_name"] = "Neighborhood FM"
        dj = create_dj(self.service, payload=payload)
        self.assertEqual(dj["station_name"], "Neighborhood FM")
        rows, _ = list_djs(self.service, station="Neighborhood")
        self.assertEqual(rows[0]["dj_id"], dj["dj_id"])

    def test_api_lists_independent_dj(self):
        dj = create_dj(self.service, payload=dict(VALID_DJ))
        _code, body = dispatch(
            self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["djs"][0]["dj_id"], dj["dj_id"])
        self.assertIsNone(body["data"]["djs"][0]["station_key"])


class TestStationIngestNeverMakesDjs(unittest.TestCase):
    """TEST 3: the radio pipeline never creates DJ records."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        self.service.close()

    def test_station_contacts_do_not_become_djs(self):
        stations = [{
            "name": "W Genre Match", "website": "https://genre-match.org",
            "station_type": "community", "genres": ["rock"],
            "country": "US", "city": "Portland",
            "confidence_score": 0.9, "status": "enriched",
            "contacts": [{"name": "DJ Behind The Decks", "role": "dj",
                          "email": "deck@genre-match.org",
                          "contact_uid": "gm_deck"}],
            "submission": None,
        }]
        self.service.ingest_intelligence(stations, source="test")
        rows, total = list_djs(self.service, limit=100)
        self.assertEqual(total, 0)
        self.assertEqual(rows, [])


class TestSearchFilters(unittest.TestCase):
    """TEST 4: listing filters target independent DJs."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))
        self.club = create_dj(self.service, payload=dict(VALID_DJ))
        self.wedding = create_dj(self.service, payload={
            "name": "DJ Simi",
            "role": "wedding_dj",
            "city": "Nairobi",
            "genres": ["soul"],
            "source_urls": ["https://events.example/simi"],
            "verification": {"fixture": True},
        })

    def tearDown(self):
        self.service.close()

    def test_dj_type_filter(self):
        rows, total = list_djs(self.service, dj_type="club_dj")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["dj_id"], self.club["dj_id"])

    def test_has_contact_filter(self):
        rows, total = list_djs(self.service, has_contact=True)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["dj_id"], self.club["dj_id"])
        rows, total = list_djs(self.service, has_contact=False)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["dj_id"], self.wedding["dj_id"])

    def test_api_supports_new_filters(self):
        _code, body = dispatch(
            self.service, "GET", "/api/v1/djs",
            {"dj_type": ["club_dj"], "has_contact": ["true"]})
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["djs"][0]["dj_id"], self.club["dj_id"])
        self.assertTrue(body["data"]["djs"][0]["has_contact"])


class TestDiscoveryPipeline(unittest.TestCase):
    """Engine-level discovery with provenance and no fabrication."""

    def setUp(self):
        fetcher = _FakeFetcher({
            "https://amara.example": {"body": GOOD_PAGE},
            "https://sontu.example": {"body": SILENT_PAGE},
            # https://dead.example intentionally absent -> dns_error
        })
        provider = StaticListProvider([
            {"name": "DJ Amara", "url": "https://amara.example",
             "city": "Lagos"},
            {"name": "DJ Sontu", "url": "https://sontu.example",
             "city": "Nairobi"},
            {"name": "DJ Ghost", "url": "https://dead.example",
             "city": "Accra"},
        ])
        request = DiscoveryRequest(query="DJ", city="Lagos", limit=10)
        self.engine = DjsDiscoveryEngine(provider, fetcher=fetcher)
        self.request = request

    def test_all_candidates_kept(self):
        result = self.engine.run(self.request)
        self.assertEqual(len(result.records), 3)

    def test_channels_keep_source_url(self):
        """TEST 5: every extracted channel retains provenance."""
        result = self.engine.run(self.request)
        amara = next(r for r in result.records
                     if r["name"] == "DJ Amara")
        self.assertTrue(amara["channels"], amara["channels"])
        for channel in amara["channels"]:
            self.assertTrue(
                channel["source_url"].startswith("https://"))
        emails = [c for c in amara["channels"]
                  if c["channel"] in ("email", "submission_email")]
        self.assertTrue(emails)
        self.assertIn("https://amara.example", amara["source_urls"])

    def test_no_contact_page_yields_no_channels(self):
        """TEST 7: no fabricated contacts when evidence is absent."""
        result = self.engine.run(self.request)
        sontu = next(r for r in result.records
                     if r["name"] == "DJ Sontu")
        self.assertEqual(sontu["channels"], [])

    def test_unreachable_site_keeps_record_without_channels(self):
        """TEST 7: site failure keeps the record but fabricates nothing."""
        result = self.engine.run(self.request)
        ghost = next(r for r in result.records
                     if r["name"] == "DJ Ghost")
        self.assertEqual(ghost["channels"], [])
        self.assertEqual(ghost["verification"]["page_fetch"], "failed")
        self.assertFalse(any(r.get("name") == "" for r in result.records))


class TestSeedIngestAndDedup(unittest.TestCase):
    """Seed population path + TESTS 6/7 (merge instead of duplicate)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "t.db"))

    def tearDown(self):
        self.service.close()

    def test_seed_ingest_creates_independent_djs(self):
        seed = _seed_file([{
            "name": "DJ Amara", "role": "club_dj", "city": "Lagos",
            "genres": ["afrobeats"], "website": "https://amara.example",
            "source_url": "https://profile.example/amara",
            "channels": [
                {"channel": "email", "value": "book@amara.example",
                 "source_url": "https://amara.example/contact"},
                {"channel": "instagram", "value": "@dj_amara",
                 "source_url": "https://amara.example/contact"},
            ],
            "verification": {"fixture": True},
        }])
        entries = load_dj_seed_entries(seed)
        self.assertEqual(len(entries), 1)
        self.assertTrue(any(c["channel"] == "website"
                            for c in entries[0]["channels"]))
        report = ingest_dj_discovery(self.service, entries)
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["failures"], [])
        rows, total = list_djs(self.service, limit=100)
        self.assertEqual(total, 1)
        self.assertIsNone(rows[0]["station_key"])
        detail = dj_detail(self.service, rows[0]["dj_id"])
        self.assertGreaterEqual(len(detail["channels"]), 3)

    def test_same_dj_discovered_twice_merges(self):
        """TEST 6: no duplicates on repeated discovery."""
        first = [{
            "name": "DJ Amara", "city": "Lagos",
            "source_urls": ["https://profile.example/amara"],
            "channels": [
                {"channel": "email", "value": "book@amara.example",
                 "source_url": "https://profile.example/amara"},
            ],
            "verification": {"fixture": True},
        }]
        second = [{
            "name": "DJ Amara", "city": "Lagos",
            "source_urls": ["https://club.example/amara"],
            "channels": [
                {"channel": "email", "value": "book@amara.example",
                 "source_url": "https://club.example/amara"},
            ],
            "verification": {"fixture": True},
        }]
        report1 = ingest_dj_discovery(self.service, first)
        report2 = ingest_dj_discovery(self.service, second)
        self.assertEqual(report1["created"], 1)
        self.assertEqual(report2["created"], 0)
        self.assertEqual(report2["merged"], 1)
        rows, total = list_djs(self.service, limit=100)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["source_urls"], [
            "https://profile.example/amara", "https://club.example/amara"])

    def test_seed_needs_provenance(self):
        """TEST 7: a channel without source_url is rejected, not stored."""
        seed = _seed_file([{
            "name": "DJ Bad Schema", "city": "Lagos",
            "channels": [{"channel": "email", "value": "x@y.example"}],
        }])
        entries = load_dj_seed_entries(seed)
        report = ingest_dj_discovery(self.service, entries)
        self.assertEqual(report["created"], 0)
        self.assertEqual(len(report["failures"]), 1)
        rows, total = list_djs(self.service, limit=100)
        self.assertEqual(total, 0)

    def test_discovery_engine_outputs_ingest(self):
        fetcher = _FakeFetcher({"https://amara.example": {"body": GOOD_PAGE}})
        engine = DjsDiscoveryEngine(
            StaticListProvider(
                [{"name": "DJ Amara", "url": "https://amara.example"}]),
            fetcher=fetcher)
        result = engine.run(DiscoveryRequest(query="DJ", limit=5))
        self.assertTrue(result.records)
        report = ingest_dj_discovery(self.service, result.records)
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["failures"], [])


if __name__ == "__main__":
    unittest.main()