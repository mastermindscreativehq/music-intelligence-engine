"""Phase 4 storage/API tests (matrix items 1–14 of the phase command).

Fully deterministic: temp-dir SQLite files and a real loopback HTTP server
on an ephemeral port. No external services, no credentials.
"""

import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from backend.api import create_server
from database.service import (
    PersistenceService,
    contact_uid,
    load_records_file,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------

def kzow_intelligence(**overrides) -> dict:
    """Rich RadioIntelligenceRecord-shaped fixture for kzow.example."""
    md_contact = {
        "id": "engine-md-uuid",
        "station_id": "engine-station-uuid",
        "name": "Alex Rivera",
        "role": "music_director",
        "email": "music@kzow.example",
        "phone": None,
        "source_url": "https://kzow.example/staff",
        "confidence_score": 0.95,
        "confidence_reasons": ["evidence source: submission_page",
                               "email on station domain"],
        "verified_at": None,
        "preferred_for_submissions": True,
        "provenance": [{
            "value": "music@kzow.example",
            "source_url": "https://kzow.example/staff",
            "source_type": "official_website_page",
            "method": "text_rule",
            "discovered_at": "",
            "also_seen_at": [],
        }],
    }
    booking_contact = {
        "id": "engine-booking-uuid", "station_id": None,
        "name": None, "role": "booking", "email": "bookings@kzow.example",
        "phone": None, "source_url": "https://kzow.example/staff",
        "confidence_score": 0.53, "confidence_reasons": ["role evidence"],
        "verified_at": None, "preferred_for_submissions": False,
        "provenance": [],
    }
    record = {
        "station_id": "engine-station-uuid",
        "organization_type": "radio_station",
        "name": "KZOW 98.3 FM",
        "alternate_names": [],
        "website": "https://kzow.example/",
        "domain": "kzow.example",
        "country": None,
        "state_or_region": None,
        "city": "Grand Junction",
        "market_area": "Grand Valley",
        "station_type": "commercial",
        "classification_confidence": 0.85,
        "classification_evidence": ["commercial keyword"],
        "formats": ["music", "talk"],
        "genres": ["jazz", "blues"],
        "genre_evidence": {"jazz": ["jazz"]},
        "language": None,
        "description": "The Valley's Jazz Station.",
        "emails": [{
            "value": "music@kzow.example",
            "source_url": "https://kzow.example/submissions",
            "source_type": "official_website_page",
            "method": "text_rule",
            "discovered_at": "",
            "also_seen_at": ["https://kzow.example/staff"],
            "quality": {"signals": ["valid_format", "own_domain",
                                    "role_inbox"],
                        "tier": "professional"},
        }],
        "phone_numbers": [],
        "contacts": [md_contact, booking_contact],
        "submission": {
            "submission_url": {
                "value": "https://kzow.example/submissions",
                "source_url": "https://kzow.example/",
                "source_type": "link_anchor_rule",
                "method": "anchor_rule",
                "discovered_at": "",
                "also_seen_at": [],
            },
            "submission_email": "music@kzow.example",
            "programming_contact_role": "music_director",
            "instructions": {
                "value": "KZOW accepts music submissions year-round.",
                "source_url": "https://kzow.example/submissions",
                "source_type": "official_website_page",
                "method": "sentence_rule",
                "discovered_at": "", "also_seen_at": [],
            },
            "restrictions": [{"restriction": "no_attachments",
                              "evidence_text": "No attachments"}],
            "methods": {"methods": ["email", "web_form"], "confidence": 0.6,
                        "reasons": ["submission email publicly listed"],
                        "kind": "inference"},
            "confidence_score": 0.9,
            "confidence_reasons": ["dedicated submission URL discovered"],
        },
        "social_urls": {"facebook": "https://www.facebook.com/kzowradio"},
        "source_urls": ["https://kzow.example/"],
        "fetches": [{"url": "https://kzow.example/", "ok": True,
                     "status": 200, "error_kind": None,
                     "fetched_at": "2026-08-21T00:00:00+00:00"}],
        "discovered_at": "2026-08-20T00:00:00+00:00",
        "last_verified_at": None,
        "last_observed_at": "2026-08-21T00:00:00+00:00",
        "confidence_score": 0.8,
        "confidence_reasons": ["reachable website",
                               "submission instructions captured"],
        "status": "enriched",
        "raw_metadata": {"enrichment_mode": "pages"},
    }
    record.update(overrides)
    return record


def wxyz_minimal(**overrides) -> dict:
    """Minimal record with a website → domain identity; most fields UNKNOWN."""
    record = {
        "station_id": "wxyz-engine-uuid",
        "organization_type": "radio_station",
        "name": "WXYZ Static Radio",
        "website": "https://wxyz.example/",
        "confidence_score": 0.35,
        "status": "enriched",
    }
    record.update(overrides)
    return record


def kzow_second_sighting() -> dict:
    """Same station via a www alias + fresh engine UUIDs + extra source URL.

    Business content of contacts is identical → same contact_uid.
    """
    base = kzow_intelligence()
    return kzow_intelligence(
        station_id="another-run-uuid",
        website="http://www.kzow.example/",
        source_urls=["http://www.kzow.example/",
                     "https://directory.example/listing/kzow"],
        contacts=[{**base["contacts"][0], "id": "brand-new-engine-uuid"}],
    )


class TempDBTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "mie_test.db")
        self.addCleanup(self._tmp.cleanup)


def make_service(case: TempDBTestCase) -> PersistenceService:
    service = PersistenceService(case.db_path)
    case.addCleanup(service.close)
    return service


# ---------------------------------------------------------------------------
# Matrix items 1–8: persistence service
# ---------------------------------------------------------------------------

class Test01StationPersistence(TempDBTestCase):
    def test_rich_record_roundtrips(self):
        service = make_service(self)
        report = service.ingest_intelligence([kzow_intelligence()],
                                             source="test")
        self.assertEqual(report.records_accepted, 1)
        self.assertEqual(report.records_failed, 0)
        row = service.get_station("domain:kzow.example")
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "KZOW 98.3 FM")
        self.assertEqual(row["identity_kind"], "domain")
        self.assertEqual(row["genres"], ["jazz", "blues"])
        self.assertEqual(row["city"], "Grand Junction")
        self.assertEqual(row["market_area"], "Grand Valley")
        self.assertEqual(row["station_type"], "commercial")
        self.assertEqual(row["status"], "enriched")

    def test_namegeo_fallback_identity_without_website(self):
        service = make_service(self)
        no_site = wxyz_minimal(website=None, country="Cedar State",
                               state_or_region="North Basin")
        report = service.ingest_intelligence([no_site], source="test")
        self.assertEqual(report.records_accepted, 1)
        rows, total = service.list_stations()
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["identity_kind"], "namegeo")


class Test02DeterministicDeduplication(TempDBTestCase):
    def test_second_sighting_updates_not_duplicates(self):
        service = make_service(self)
        first = kzow_intelligence()
        r1 = service.ingest_intelligence([first], source="test")
        r2 = service.ingest_intelligence([kzow_second_sighting()],
                                         source="test")
        self.assertEqual(r1.stations_upserted, 1)
        self.assertEqual(r2.stations_upserted, 1)
        rows, total = service.list_stations()
        self.assertEqual(total, 1)
        row = rows[0]
        # Provenance unions rather than overwrites:
        self.assertIn("https://directory.example/listing/kzow",
                      row["source_urls"])
        self.assertEqual(row["discovered_at"], first["discovered_at"])
        # Same business content despite new engine id → single contact row
        # sharing one deterministic uid:
        expected_uid = contact_uid("domain:kzow.example",
                                   first["contacts"][0])
        md_rows = [c for c in
                   service.get_station_contacts("domain:kzow.example")
                   if c["contact_uid"] == expected_uid]
        self.assertEqual(len(md_rows), 1)


class Test03ContactPersistence(TempDBTestCase):
    def test_contacts_stable_and_flagged(self):
        service = make_service(self)
        service.ingest_intelligence([kzow_intelligence()], source="test")
        contacts = service.get_station_contacts("domain:kzow.example")
        self.assertEqual(len(contacts), 2)
        by_email = {c["email"]: c for c in contacts}
        md = by_email["music@kzow.example"]
        booking = by_email["bookings@kzow.example"]
        self.assertTrue(md["preferred_for_submissions"])
        self.assertFalse(booking["preferred_for_submissions"])
        self.assertEqual(md["name"], "Alex Rivera")
        self.assertEqual(md["role"], "music_director")
        self.assertIsNone(booking["name"])

    def test_contact_uid_case_insensitive_on_email_and_name(self):
        a = contact_uid("domain:x.example", {"email": "MUSIC@X.EXAMPLE",
                                             "name": "Alex Rivera"})
        b = contact_uid("domain:x.example", {"email": "music@x.example",
                                             "name": "alex rivera"})
        self.assertEqual(a, b)


class Test04SubmissionPathPersistence(TempDBTestCase):
    def test_payload_verbatim_with_inference_label(self):
        service = make_service(self)
        original = kzow_intelligence()["submission"]
        service.ingest_intelligence([kzow_intelligence()], source="test")
        stored = service.get_submission("domain:kzow.example")
        self.assertEqual(stored, original)
        self.assertEqual(stored["methods"]["kind"], "inference")


class Test05ProvenancePersistence(TempDBTestCase):
    def test_facts_keep_provenance_verbatim(self):
        service = make_service(self)
        original_fact = kzow_intelligence()["emails"][0]
        service.ingest_intelligence([kzow_intelligence()], source="test")
        facts = service.get_station_emails("domain:kzow.example")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0], original_fact)
        fetches = service.get_fetches("domain:kzow.example")
        self.assertEqual(fetches[0]["url"], "https://kzow.example/")
        self.assertTrue(fetches[0]["ok"])


class Test06RepeatedIngestionIdempotency(TempDBTestCase):
    def test_double_ingest_is_safe(self):
        service = make_service(self)
        payload = [kzow_intelligence(), wxyz_minimal()]
        service.ingest_intelligence(payload, source="test")
        before = service.get_station_contacts("domain:kzow.example")
        before_prov = before[0]["provenance"]
        report = service.ingest_intelligence(list(payload), source="test")
        self.assertEqual(report.records_accepted, 2)
        self.assertEqual(report.records_failed, 0)
        _, total = service.list_stations()
        self.assertEqual(total, 2)
        after = service.get_station_contacts("domain:kzow.example")
        self.assertEqual(len(after), len(before))
        self.assertEqual(after[0]["provenance"], before_prov)


class Test07NullUnknownHandling(TempDBTestCase):
    def test_nulls_never_erase_known_values(self):
        service = make_service(self)
        service.ingest_intelligence([kzow_intelligence()], source="test")
        service.ingest_intelligence(
            [kzow_intelligence(city=None, description=None)], source="test")
        row = service.get_station("domain:kzow.example")
        self.assertEqual(row["city"], "Grand Junction")
        self.assertEqual(row["description"], "The Valley's Jazz Station.")
        absent = kzow_intelligence()
        del absent["language"]
        service.ingest_intelligence([absent], source="test")
        row = service.get_station("domain:kzow.example")
        self.assertEqual(row["city"], "Grand Junction")

    def test_unknown_stays_unknown_in_minimal_record(self):
        service = make_service(self)
        service.ingest_intelligence([wxyz_minimal()], source="test")
        row = service.get_station("domain:wxyz.example")
        self.assertIsNotNone(row)
        for field in ("country", "state_or_region", "city", "market_area",
                      "language", "description", "last_verified_at"):
            self.assertIsNone(row[field])
        self.assertIsNone(row["station_type"])


class Test08ConfidencePreservation(TempDBTestCase):
    def test_scores_and_reasons_survive_roundtrip(self):
        service = make_service(self)
        original = kzow_intelligence()
        service.ingest_intelligence([original], source="test")
        row = service.get_station("domain:kzow.example")
        self.assertEqual(row["confidence_score"], 0.8)
        self.assertEqual(row["confidence_reasons"],
                         original["confidence_reasons"])
        md = next(c for c in
                  service.get_station_contacts("domain:kzow.example")
                  if c["email"] == "music@kzow.example")
        self.assertEqual(md["confidence_score"], 0.95)
        self.assertTrue(md["confidence_reasons"])

    def test_out_of_range_confidence_rejected(self):
        service = make_service(self)
        report = service.ingest_intelligence(
            [kzow_intelligence(confidence_score=1.5)], source="test")
        self.assertEqual(report.records_failed, 1)
        self.assertEqual(report.failures[0]["error_kind"], "ValidationError")


# ---------------------------------------------------------------------------
# Matrix items 9–12: HTTP API
# ---------------------------------------------------------------------------

class APIServerHarness:
    """Real loopback server on an ephemeral port; urllib client."""

    def __init__(self, db_path: str):
        self.server = create_server(db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.service.close()
        self.server.server_close()

    def get(self, path: str, method: str = "GET"):
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class ApiTestCase(TempDBTestCase):
    def setUp(self):
        super().setUp()
        self.harness = APIServerHarness(self.db_path)
        self.harness.server.service.ingest_intelligence(
            [kzow_intelligence(), wxyz_minimal()], source="test")
        self.harness.start()
        self.addCleanup(self.harness.stop)

    def get(self, path, method="GET"):
        return self.harness.get(path, method=method)


class Test09ApiStationListing(ApiTestCase):
    def test_list_envelope_projection_and_filters(self):
        status, body = self.get("/api/v1/stations")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(body["error"])
        data = body["data"]
        self.assertEqual(data["total"], 2)
        first = data["stations"][0]
        for field in ("identity_key", "name", "website", "station_type",
                      "confidence_score", "status", "genres", "links"):
            self.assertIn(field, first)
        _status, filtered = self.get("/api/v1/stations?q=kzow")
        self.assertEqual(filtered["data"]["total"], 1)
        _status, by_status = self.get("/api/v1/stations?status=enriched")
        self.assertEqual(by_status["data"]["total"], 2)
        status_p, paged = self.get("/api/v1/stations?limit=1&offset=1")
        self.assertEqual(status_p, 200)
        self.assertEqual(paged["data"]["limit"], 1)
        self.assertEqual(len(paged["data"]["stations"]), 1)

    def test_health_endpoint(self):
        status, body = self.get("/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["schema_version"], 1)

    def test_bad_params_rejected(self):
        for query in ("limit=abc", "limit=0", "limit=5000", "offset=-1"):
            with self.subTest(query=query):
                status, body = self.get(f"/api/v1/stations?{query}")
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertEqual(body["error"]["code"], "bad_request")


class Test10ApiStationRetrieval(ApiTestCase):
    def test_detail_by_identity_key(self):
        status, body = self.get("/api/v1/stations/domain:kzow.example")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["name"], "KZOW 98.3 FM")
        self.assertIn("first_stored_at", data)
        self.assertIn("raw_metadata", data)

    def test_unknown_station_404_envelope(self):
        status, body = self.get("/api/v1/stations/domain:nosuch.example")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "station_not_found")

    def test_unknown_route_404_and_post_405(self):
        status, body = self.get("/api/v1/nowhere")
        self.assertEqual((status, body["error"]["code"]),
                         (404, "route_not_found"))
        status, body = self.get("/api/v1/stations", method="POST")
        self.assertEqual(status, 405)


class Test11ApiIntelligenceRetrieval(ApiTestCase):
    def test_full_view_distinguishes_fact_inference_unknown(self):
        status, body = self.get(
            "/api/v1/stations/domain:kzow.example/intelligence")
        self.assertEqual(status, 200)
        data = body["data"]
        epi = data["epistemology"]
        self.assertGreaterEqual(epi["facts_count"], 3)
        self.assertEqual(epi["inferred_fields"], ["submission.methods"])
        self.assertNotIn("country", epi["unknown_fields"])
        self.assertNotIn("submission", epi["unknown_fields"])
        # Email Fact dicts survive verbatim through storage + API:
        self.assertEqual(data["emails"][0]["source_url"],
                         "https://kzow.example/submissions")
        self.assertEqual(data["emails"][0]["method"], "text_rule")
        # Inference bundle keeps its label end-to-end:
        self.assertEqual(data["submission"]["methods"]["kind"], "inference")

    def test_minimal_station_reports_unknown_fields(self):
        status, body = self.get(
            "/api/v1/stations/domain:wxyz.example/intelligence")
        self.assertEqual(status, 200)
        data = body["data"]
        for field in ("country", "city", "market_area", "description",
                      "emails", "contacts", "submission"):
            self.assertIn(field, data["epistemology"]["unknown_fields"])
        self.assertEqual(data["emails"], [])


class Test12ApiContactsAndSubmission(ApiTestCase):
    def test_contacts_payload_with_preferred_and_submission(self):
        status, body = self.get(
            "/api/v1/stations/domain:kzow.example/contacts")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(len(data["contacts"]), 2)
        preferred = data["preferred_submission_contacts"]
        self.assertEqual(len(preferred), 1)
        self.assertEqual(preferred[0]["role"], "music_director")
        self.assertEqual(preferred[0]["email"], "music@kzow.example")
        self.assertEqual(data["submission"]["programming_contact_role"],
                         "music_director")
        self.assertEqual(data["submission"]["methods"]["methods"],
                         ["email", "web_form"])


# ---------------------------------------------------------------------------
# Matrix items 13–14: malformed input + secret leakage prevention
# ---------------------------------------------------------------------------

class Test13MalformedInputRejection(TempDBTestCase):
    CASES = [
        ("non-object record", 42),
        ("empty name", kzow_intelligence(name="   ")),
        ("missing name", {k: v for k, v in kzow_intelligence().items()
                          if k != "name"}),
        ("unsupported org type", kzow_intelligence(organization_type="label")),
        ("bad website scheme", kzow_intelligence(website="ftp://x.example")),
        ("non-list contacts", kzow_intelligence(contacts="many")),
        ("non-dict contact", kzow_intelligence(contacts=["md"])),
        ("bad contact score", kzow_intelligence(contacts=[
            {**kzow_intelligence()["contacts"][0],
             "confidence_score": 2.0}])),
        ("submission wrong type", kzow_intelligence(submission="yes")),
    ]

    def test_each_case_isolated_as_validation_failure(self):
        service = make_service(self)
        for label, record in self.CASES:
            with self.subTest(case=label):
                report = service.ingest_intelligence([record], source="test")
                self.assertEqual(report.records_failed, 1)
                failure = report.failures[0]
                self.assertEqual(failure["stage"], "validation")
                self.assertEqual(failure["error_kind"], "ValidationError")
                self.assertTrue(failure["message"])

    def test_mixed_batch_isolates_failures(self):
        service = make_service(self)
        report = service.ingest_intelligence(
            [42, kzow_intelligence(), wxyz_minimal(name="")], source="test")
        self.assertEqual(report.records_accepted, 1)
        self.assertEqual(report.records_failed, 2)
        self.assertIsNotNone(service.get_station("domain:kzow.example"))

    def test_loader_rejects_wrong_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"records": 5}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_records_file(str(bad))
            bad.write_text(json.dumps({"unrelated": True}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_records_file(str(bad))


class Test14SecretLeakagePrevention(TempDBTestCase):
    SECRET = "phase4-canary-value-NOT-A-REAL-CREDENTIAL"
    SECRET_PATTERNS = [
        r"sk-[A-Za-z0-9]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"postgres(?:ql)?://\S+:\S+@",
    ]

    def test_env_value_never_reaches_responses_or_sources(self):
        os.environ["MIE_TEST_CANARY"] = self.SECRET
        self.addCleanup(os.environ.pop, "MIE_TEST_CANARY", None)
        harness = APIServerHarness(self.db_path)
        harness.server.service.ingest_intelligence(
            [kzow_intelligence(), wxyz_minimal()], source="test")
        harness.start()
        self.addCleanup(harness.stop)

        paths = ("/api/v1/health",
                 "/api/v1/stations",
                 "/api/v1/stations/domain:kzow.example",
                 "/api/v1/stations/domain:kzow.example/intelligence",
                 "/api/v1/stations/domain:kzow.example/contacts",
                 "/api/v1/stations/domain:nosuch.example")
        for path in paths:
            _status, body = harness.get(path)
            blob = json.dumps(body)
            self.assertNotIn(self.SECRET, blob, path)
            self.assertNotIn("MIE_TEST_CANARY", blob, path)

    def test_new_sources_have_no_credential_shaped_strings(self):
        for rel in ("backend/api.py", "backend/contracts.py",
                    "database/service.py", "database/schema.py",
                    "database/__init__.py", "backend/__init__.py"):
            source = (ROOT / rel).read_text(encoding="utf-8")
            for pattern in self.SECRET_PATTERNS:
                match = re.search(pattern, source)
                self.assertIsNone(match,
                                  f"{rel} matches {pattern}: "
                                  f"{match.group(0) if match else ''}")


if __name__ == "__main__":
    unittest.main()
