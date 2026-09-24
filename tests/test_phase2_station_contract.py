"""Phase 2: universal station intelligence tests.

Covers the audited, additive Phase 2 behavior only:

- Candidate location carry-through (discovery.models / discovery.providers);
- pipeline location assignment + location_evidence provenance;
- intelligence location merge (market_area carry, provenance synthesis);
- canonical station contract (location normalization + honest status);
- backend contract projections gain additive ``location_status``;
- generic send-music route recognition (submission intelligence, no outreach);
- storage round-trip preserves location.

Deterministic and offline: FakeFetcher serves fixture HTML, providers are
seed-file/static backed, storage is a temp-dir SQLite file.
"""

import json
import tempfile
import unittest
from pathlib import Path

from crawler.http import FetchResult
from crawler.pages import parse_html
from crawler.urls import normalize_url

from backend.contracts import station_detail, station_summary
from database.service import PersistenceService
from discovery.models import Candidate, DiscoveryRequest
from discovery.providers import SeedListProvider, StaticListProvider
from discovery.radio import contract as station_contract
from discovery.radio.contract import (
    LOCATION_AVAILABLE,
    LOCATION_UNAVAILABLE,
    LOCATION_UNVERIFIED,
    canonical_location,
    derive_location_status,
)
from discovery.radio.intelligence import build_intelligence_record
from discovery.radio.pipeline import EngineConfig, RadioDiscoveryEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGES = FIXTURES / "pages"


def load(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


class FakeFetcher:
    """URL-keyed fetcher double (same contract as test_pipeline.py)."""

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


def default_pages() -> dict:
    return {
        "https://kqxr.example/": {"body": load("kqxr_home.html")},
        "https://kqxr.example/contact": {"body": load("kqxr_contact.html")},
        "https://kqxr.example/submissions": {"body": load("kqxr_submissions.html")},
    }


def make_engine(pages=None):
    return RadioDiscoveryEngine(
        provider=None,
        fetcher=FakeFetcher(pages or default_pages()),
        config=EngineConfig(crawl_delay_seconds=0.0, respect_robots=False,
                            max_pages_per_site=6),
    )


def run_with(provider_entries, pages=None, **request_kwargs):
    engine = make_engine(pages)
    engine.provider = StaticListProvider(provider_entries)
    request_kwargs.setdefault("query", "radio stations")
    request = DiscoveryRequest.from_dict(request_kwargs)
    return engine.run(request)


# ---------------------------------------------------------------------------
# Candidate location fields (discovery.models)
# ---------------------------------------------------------------------------

class TestCandidateLocation(unittest.TestCase):
    def test_defaults_to_none(self):
        candidate = Candidate(title="KEXP", url="https://kexp.org/",
                              source="seed_file:seeds.json")
        self.assertIsNone(candidate.country)
        self.assertIsNone(candidate.state_or_region)
        self.assertIsNone(candidate.city)

    def test_values_kept_and_serialized(self):
        candidate = Candidate(
            title="KEXP", url="https://kexp.org/", source="seed",
            country="United States", state_or_region="Washington",
            city="Seattle")
        data = candidate.to_dict()
        self.assertEqual(data["country"], "United States")
        self.assertEqual(data["state_or_region"], "Washington")
        self.assertEqual(data["city"], "Seattle")

    def test_empty_strings_normalized_to_none(self):
        candidate = Candidate(title="KEXP", url="https://kexp.org/",
                              source="seed", country="   ")
        self.assertIsNone(candidate.country)

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            Candidate(title="KEXP", url="https://kexp.org/",
                      source="seed", country=12)


# ---------------------------------------------------------------------------
# Provider carry-through (discovery.providers)
# ---------------------------------------------------------------------------

class TestProviderLocationCarry(unittest.TestCase):
    def test_seed_list_provider_carries_geography(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed.json"
            seed.write_text(json.dumps({"stations": [
                {"name": "KEXP 90.3 FM", "url": "https://www.kexp.org/",
                 "country": "United States",
                 "state_or_region": "Washington"},
            ]}), encoding="utf-8")
            provider = SeedListProvider(seed)
            candidates = provider.search(
                DiscoveryRequest.from_dict({"query": "radio stations"}),
                ["college radio"])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].country, "United States")
            self.assertEqual(candidates[0].state_or_region, "Washington")

    def test_static_list_provider_carries_geography(self):
        provider = StaticListProvider([
            {"name": "WFMU", "url": "https://wfmu.org/",
             "country": "United States", "state_or_region": "New Jersey",
             "city": "Jersey City"},
        ])
        request = DiscoveryRequest.from_dict({"query": "radio stations"})
        candidate = provider.search(request, [])[0]
        self.assertEqual(candidate.city, "Jersey City")
        self.assertEqual(candidate.state_or_region, "New Jersey")
        self.assertEqual(candidate.country, "United States")


# ---------------------------------------------------------------------------
# Pipeline location assignment + evidence (discovery.radio.pipeline)
# ---------------------------------------------------------------------------

class TestPipelineLocationCarry(unittest.TestCase):
    def test_seed_location_reaches_record_with_evidence(self):
        result = run_with([
            {"name": "KQXR 101.5 FM", "url": "https://kqxr.example/",
             "country": "United States", "state_or_region": "Oregon",
             "city": "Portland"},
        ])
        self.assertEqual(result.record_count, 1)
        record = result.records[0]
        # Country now derives from the station's own title/callsign ("KQXR
        # 101.5 FM"); region and city, which the site's pages do not name,
        # are filled by the seed attached to this candidate (fallback only).
        self.assertEqual(record["country"], "United States")
        self.assertEqual(record["state_or_region"], "Oregon")
        self.assertEqual(record["city"], "Portland")
        evidence = record["raw_metadata"]["location_evidence"]
        self.assertTrue(evidence)
        country = next(e for e in evidence if e["field"] == "country")
        self.assertEqual(country["value"], "United States")
        self.assertEqual(country["source_type"], "official_website_page")
        self.assertEqual(country["method"], "callsign_rule")
        self.assertNotEqual(country.get("discovered_at"), "")
        region = next(e for e in evidence if e["field"] == "state_or_region")
        self.assertEqual(region["value"], "Oregon")
        self.assertEqual(region["source_type"], "seed_data")
        self.assertEqual(region["method"], "carry_through")
        self.assertEqual(region["source_url"], "https://kqxr.example/")
        # The discovery request's scope is never part of the evidence.
        self.assertFalse(any(
            e["source_type"] == "discovery_request" for e in evidence))

    def test_request_geography_is_never_carried(self):
        # A request scoped to "United States" / "Oregon" must NOT imprint the
        # station record: the site's callsign says US, but no region/city is
        # asserted from the request, and none is invented.
        result = run_with([
            {"name": "KQXR 101.5 FM", "url": "https://kqxr.example/"},
        ], country="United States", state_or_region="Oregon")
        record = result.records[0]
        self.assertEqual(record["country"], "United States")
        self.assertIsNone(record["state_or_region"])
        self.assertIsNone(record["city"])
        evidence = record["raw_metadata"]["location_evidence"]
        self.assertFalse(any(e["source_type"] == "discovery_request"
                             for e in evidence))
        self.assertTrue(any(e["method"] == "callsign_rule" for e in evidence))

    def test_absent_location_stays_absent(self):
        result = run_with(
            [{"name": "Untitled Station", "url": "https://nogeo.example/"}],
            pages={"https://nogeo.example/": {"body":
                "<html><head><title>Untitled Station</title></head><body>"
                "<p>Independent radio, on your dial 24/7 since 1997.</p>"
                "</body></html>"}},
        )
        record = result.records[0]
        self.assertIsNone(record["country"])
        self.assertIsNone(record["state_or_region"])
        self.assertIsNone(record["city"])
        self.assertNotIn("location_evidence",
                         record["raw_metadata"])


# ---------------------------------------------------------------------------
# Intelligence location merge (discovery.radio.intelligence)
# ---------------------------------------------------------------------------

class TestIntelligenceLocationMerge(unittest.TestCase):
    def test_location_and_market_carry_without_pages(self):
        station = {
            "id": "s1",
            "name": "KQXR 101.5 FM",
            "website": "https://kqxr.example/",
            "source_urls": ["https://kqxr.example/"],
            "country": "United States",
            "state_or_region": "Oregon",
            "city": "Portland",
            "market_area": "PDX metro",
            "raw_metadata": {"location_evidence": [{
                "value": "Portland", "field": "city",
                "source_url": "https://kqxr.example/",
                "source_type": "seed_data", "method": "carry_through",
                "discovered_at": "",
            }]},
        }
        record = build_intelligence_record(station, pages=[],
                                           fetch_records=None)
        self.assertEqual(record.country, "United States")
        self.assertEqual(record.city, "Portland")
        self.assertEqual(record.market_area, "PDX metro")
        evidence = record.raw_metadata["location_evidence"]
        self.assertEqual(len(evidence), 3)
        city = next(e for e in evidence if e["field"] == "city")
        self.assertEqual(city["method"], "carry_through")
        state = next(e for e in evidence if e["field"] == "state_or_region")
        self.assertEqual(state["method"], "observed")
        self.assertEqual(state["source_url"], "https://kqxr.example/")

    def test_no_location_means_no_evidence(self):
        station = {"id": "s1", "name": "KQXR", "website": "https://kqxr.example/",
                   "source_urls": ["https://kqxr.example/"],
                   "raw_metadata": {}}
        record = build_intelligence_record(station, pages=[],
                                           fetch_records=None)
        self.assertIsNone(record.country)
        self.assertEqual(record.raw_metadata["location_evidence"], [])
        self.assertEqual(derive_location_status(
            record.country, record.state_or_region, record.city,
            record.market_area),
            LOCATION_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Generic send-music route recognition (no outreach records)
# ---------------------------------------------------------------------------

class TestSubmissionRouteRecognition(unittest.TestCase):
    HTML = (
        "<html><head><title>Example Community Radio</title></head><body>"
        "<p>Welcome to our station.</p>"
        "<a href=\"https://example-station.test/sendmusic\">"
        "Send us your music</a>"
        "</body></html>"
    )

    def test_send_music_page_promoted_to_submission_intelligence(self):
        page = parse_html("https://example-station.test/", self.HTML)
        station = {
            "id": "c1",
            "name": "Example Community Radio",
            "website": "https://example-station.test/",
            "source_urls": ["https://example-station.test/"],
            "submission_url": None,
            "contacts": [],
            "emails": [],
            "raw_metadata": {},
        }
        record = build_intelligence_record(station, pages=[page],
                                           fetch_records=None)
        self.assertIsNotNone(record.submission)
        self.assertEqual(record.submission.submission_url["value"],
                         "https://example-station.test/sendmusic")
        self.assertEqual(record.submission.submission_url["method"], "link")
        self.assertIn("published send-music page identified",
                      record.submission.confidence_reasons)

    def test_no_send_music_page_is_honest_unknown(self):
        page = parse_html(
            "https://quiet.example/",
            "<html><head><title>Quiet</title></head><body>"
            "<p>Just a schedule.</p></body></html>")
        station = {"id": "c2", "name": "Quiet",
                   "website": "https://quiet.example/",
                   "source_urls": ["https://quiet.example/"],
                   "raw_metadata": {}}
        record = build_intelligence_record(station, pages=[page],
                                           fetch_records=None)
        self.assertIsNone(record.submission)


# ---------------------------------------------------------------------------
# Canonical contract (discovery.radio.contract)
# ---------------------------------------------------------------------------

class TestCanonicalStationContract(unittest.TestCase):
    def test_status_unavailable_when_nothing_recorded(self):
        self.assertEqual(derive_location_status(), LOCATION_UNAVAILABLE)
        self.assertEqual(canonical_location({}),
                         {"status": LOCATION_UNAVAILABLE})

    def test_status_unverified_without_verified_timestamp(self):
        self.assertEqual(derive_location_status(country="United States"),
                         LOCATION_UNVERIFIED)

    def test_status_available_when_verified(self):
        self.assertEqual(derive_location_status(
            country="United States", state_or_region="Washington",
            city="Seattle", verified_at="2026-09-10T00:00:00+00:00"),
            LOCATION_AVAILABLE)

    def test_canonical_location_keeps_only_present_values(self):
        location = canonical_location({
            "country": "United States",
            "state_or_region": "Washington",
            "city": "Seattle",
            "market_area": None,
        })
        self.assertEqual(location["city"], "Seattle")
        self.assertNotIn("market_area", location)
        self.assertEqual(location["status"], LOCATION_UNVERIFIED)

    def test_location_evidence_returns_clean_copies(self):
        station = {"raw_metadata": {"location_evidence": [
            {"value": "Seattle", "field": "city", "source_url": "https://x/"},
        ]}}
        evidence = station_contract.location_evidence(station)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["value"], "Seattle")
        self.assertEqual(station_contract.location_evidence({}), [])


# ---------------------------------------------------------------------------
# Backend contract projections (backend.contracts) — additive only
# ---------------------------------------------------------------------------

class TestBackendContractAdditiveLocation(unittest.TestCase):
    ROW = {
        "identity_key": "domain:kqwh.example",
        "identity_kind": "domain",
        "name": "KQWH",
        "organization_type": "radio_station",
        "website": "https://kqwh.example/",
        "domain": "kqwh.example",
        "country": "United States",
        "state_or_region": "Michigan",
        "city": "Kalamazoo",
        "market_area": "SW Michigan",
        "station_type": "college",
        "confidence_score": 0.4,
        "status": "enriched",
        "genres": [],
        "formats": [],
        "discovered_at": "",
        "last_observed_at": "",
        "last_verified_at": None,
    }

    def test_summary_includes_location_status_additively(self):
        summary = station_summary(self.ROW)
        self.assertEqual(summary["location_status"], LOCATION_UNVERIFIED)
        for field in ("identity_key", "name", "genres", "formats",
                      "confidence_score", "links"):
            self.assertIn(field, summary)

    def test_detail_includes_location_status_additively(self):
        detail = station_detail(self.ROW)
        self.assertEqual(detail["location_status"], LOCATION_UNVERIFIED)
        self.assertIn("links", detail)

    def test_detail_available_when_verified(self):
        row = dict(self.ROW)
        row["last_verified_at"] = "2026-09-10T00:00:00+00:00"
        self.assertEqual(station_detail(row)["location_status"],
                         LOCATION_AVAILABLE)

    def test_unavailable_when_location_absent(self):
        row = dict(self.ROW)
        for field in ("country", "state_or_region", "city", "market_area"):
            row[field] = None
        self.assertEqual(station_summary(row)["location_status"],
                         LOCATION_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Storage round-trip preserves location (database.service)
# ---------------------------------------------------------------------------

class TestStorageLocationRoundTrip(unittest.TestCase):
    def test_location_persists_and_reads_back(self):
        record = {
            "station_id": "engine-loc-uuid",
            "organization_type": "radio_station",
            "name": "KZLX 100.5 FM",
            "alternate_names": [],
            "website": "https://kzlx.example/",
            "domain": "kzlx.example",
            "country": "Canada",
            "state_or_region": "British Columbia",
            "city": "Vancouver",
            "market_area": "Lower Mainland",
            "station_type": "campus",
            "classification_confidence": 0.5,
            "classification_evidence": [],
            "formats": ["music"],
            "genres": ["jazz"],
            "genre_evidence": {},
            "language": "english",
            "description": None,
            "emails": [],
            "phone_numbers": [],
            "contacts": [],
            "submission": None,
            "useful_pages": [],
            "social_urls": {},
            "source_urls": ["https://kzlx.example/"],
            "fetches": [],
            "discovered_at": "",
            "last_verified_at": None,
            "last_observed_at": "",
            "confidence_score": 0.2,
            "confidence_reasons": [],
            "status": "enriched",
            "raw_metadata": {"location_evidence": [
                {"value": "Vancouver", "field": "city",
                 "source_url": "https://kzlx.example/",
                 "source_type": "seed_data", "method": "carry_through",
                 "discovered_at": ""},
            ]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "phase2.db")
            storage = PersistenceService(db)
            try:
                storage.ingest_intelligence([record], source="phase2-test")
                rows, _ = storage.list_stations(limit=10)
                self.assertEqual(rows[0]["city"], "Vancouver")
                self.assertEqual(rows[0]["country"], "Canada")
                self.assertTrue(
                    rows[0]["raw_metadata"]["location_evidence"])
                self.assertEqual(
                    station_detail(rows[0])["location_status"],
                    LOCATION_UNVERIFIED)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()