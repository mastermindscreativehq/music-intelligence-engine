"""Phase 3 enrichment/intelligence tests (matrix A–T).

No network access: engines run offline or against injected fake fetchers.
Every mandated behavior gets a labeled test method (A through T).
"""

import json
import tempfile
import unittest
from pathlib import Path

from crawler.http import FetchResult
from crawler.pages import parse_html

from discovery.models import EnrichmentResult
from discovery.radio.enrich_pipeline import EnrichmentEngine, main
from discovery.radio.intelligence import build_intelligence_record
from discovery.radio.schema import (
    EnrichedContact,
    RadioIntelligenceRecord,
    SourceFetchRecord,
    SubmissionPath,
)

from enrichment.confidence import score_contact
from enrichment.formats import detect_formats, detect_genres, extract_market
from enrichment.roles import ROLE_VOCABULARY, classify_role
from enrichment.submissions import (
    detect_restrictions,
    extract_submission_instructions,
    infer_submission_methods,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGES_DIR = FIXTURES / "pages"


def load(name: str) -> str:
    return (PAGES_DIR / name).read_text(encoding="utf-8")


KZOW_HOME = load("kzow_home.html")
KZOW_STAFF = load("kzow_staff.html")
KZOW_SUBMIT = load("kzow_submit.html")

SUBMIT_URL = "https://kzow.example/submissions"
STAFF_URL = "https://kzow.example/staff"
HOME_URL = "https://kzow.example/"


def parsed(url: str, html: str):
    return parse_html(url, html)


def kzow_station(**overrides) -> dict:
    """A realistic Phase 2 StationRecord.to_dict() for KZOW."""
    record = {
        "id": "11111111-1111-1111-1111-111111111111",
        "organization_type": "radio_station",
        "name": "KZOW 98.3 FM",
        "alternate_names": [],
        "legal_name": None,
        "website": HOME_URL,
        "country": None,
        "state_or_region": None,
        "city": None,
        "station_type": "unknown",
        "classification_confidence": 0.0,
        "classification_evidence": [],
        "format": None,
        "genres": [],
        "language": None,
        "description": None,
        "emails": [{
            "value": "info@kzow.example",
            "source_url": HOME_URL,
            "source_type": "official_website_page",
            "method": "text_rule",
            "discovered_at": "",
            "also_seen_at": [],
        }],
        "contacts": [],
        "phone_numbers": [],
        "submission_url": {
            "value": SUBMIT_URL,
            "source_url": HOME_URL,
            "source_type": "link_anchor_rule",
            "method": "anchor_rule",
            "discovered_at": "",
            "also_seen_at": [],
        },
        "contact_url": {
            "value": STAFF_URL,
            "source_url": HOME_URL,
            "source_type": "link_anchor_rule",
            "method": "anchor_rule",
            "discovered_at": "",
            "also_seen_at": [],
        },
        "programming_url": None,
        "social_urls": {},
        "source_urls": [HOME_URL],
        "website_reachable": True,
        "name_matches_site": True,
        "discovered_at": "2026-08-20T00:00:00+00:00",
        "last_verified_at": None,
        "last_observed_at": "2026-08-20T00:00:00+00:00",
        "confidence_score": 0.7,
        "confidence_reasons": ["reachable website"],
        "status": "discovered",
        "raw_metadata": {"homepage_title": "KZOW 98.3 FM"},
    }
    record.update(overrides)
    return record


class FakeFetcher:
    """Exact-URL fetcher double serving fixture HTML."""

    def __init__(self, pages: dict[str, str]):
        self.pages = dict(pages)
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        result = FetchResult(url=url)
        body = self.pages.get(url)
        if body is None:
            result.error_kind = "dns_error"
            result.error_message = "fixture miss"
            return result
        result.status = 200
        result.content_type = "text/html"
        result.body = body
        result.final_url = url
        return result


def make_engine(pages: dict[str, str] | None = None) -> tuple[EnrichmentEngine, FakeFetcher]:
    fetcher = FakeFetcher(pages or {
        SUBMIT_URL: KZOW_SUBMIT,
        STAFF_URL: KZOW_STAFF,
        HOME_URL: KZOW_HOME,
    })
    return EnrichmentEngine(fetcher=fetcher), fetcher


# ---------------------------------------------------------------------------
# A–G: enrichment primitives
# ---------------------------------------------------------------------------

class TestARoleVocabulary(unittest.TestCase):
    def test_new_roles_classified(self):
        self.assertEqual(classify_role("Music Programmer"), "music_programmer")
        self.assertEqual(classify_role("executive producer"), "producer")
        self.assertEqual(classify_role("Morning Show Host"), "host")
        self.assertEqual(classify_role("Media inquiries"), "media")
        self.assertEqual(classify_role("Bookings"), "booking")

    def test_vocabulary_contains_all_roles(self):
        for role in ("music_programmer", "producer", "host", "media",
                     "booking", "music_director", "unknown"):
            self.assertIn(role, ROLE_VOCABULARY)


class TestBGenreDetection(unittest.TestCase):
    def test_ranking_counts_and_cap(self):
        genres, evidence = detect_genres([KZOW_HOME])
        self.assertIn("jazz", genres)
        self.assertIn("blues", genres)
        self.assertGreaterEqual(len(genres), 3)
        limited, _ = detect_genres([KZOW_HOME], max_genres=2)
        self.assertEqual(len(limited), 2)
        # Evidence explains every reported genre.
        for genre in genres:
            self.assertTrue(evidence[genre])


class TestCFormatDetection(unittest.TestCase):
    def test_music_and_talk_cues(self):
        formats, evidence = detect_formats([KZOW_HOME])
        self.assertIn("music", formats)
        self.assertTrue(evidence["music"])


class TestDMarketExtraction(unittest.TestCase):
    def test_explicit_claim_captured(self):
        self.assertEqual(extract_market([KZOW_HOME]), "Grand Valley")

    def test_absent_claim_stays_unknown(self):
        self.assertIsNone(
            extract_market(["The best music in town, whatever that means."]))


class TestESubmissionInstructions(unittest.TestCase):
    def test_captures_instruction_sentences(self):
        snippet = extract_submission_instructions(KZOW_SUBMIT)
        self.assertIsNotNone(snippet)
        self.assertIn("accepts music submissions", snippet)

    def test_bounded_and_none_when_unrelated(self):
        long_text = ". ".join(["Submit your music today"] * 60) + "."
        bounded = extract_submission_instructions(long_text)
        self.assertLessEqual(len(bounded), 400)
        self.assertIsNone(
            extract_submission_instructions("Weather tomorrow: sunny."))


class TestFSubmissionRestrictions(unittest.TestCase):
    def test_restriction_tokens_found(self):
        restrictions = {
            r["restriction"]: r["evidence_text"]
            for r in detect_restrictions(KZOW_SUBMIT)
        }
        self.assertIn("no_attachments", restrictions)
        self.assertIn("digital_only", restrictions)
        self.assertIn("no_phone_calls", restrictions)
        self.assertIn("review_window", restrictions)

    def test_plain_text_has_no_restrictions(self):
        self.assertEqual(detect_restrictions("We love new tracks."), [])


class TestGMethodInference(unittest.TestCase):
    def test_email_and_form_inferred_and_labeled(self):
        bundle = infer_submission_methods(
            has_form=True, submission_email="music@kzow.example",
            texts=["Submit your music via the form below."])
        self.assertEqual(bundle["kind"], "inference")
        self.assertIn("email", bundle["methods"])
        self.assertIn("web_form", bundle["methods"])
        self.assertTrue(bundle["reasons"])

    def test_postal_inference(self):
        bundle = infer_submission_methods(
            has_form=False, submission_email=None,
            texts=["Send demos by mail only to our studio address."])
        self.assertEqual(bundle["methods"], ["postal"])

    def test_no_evidence_no_methods(self):
        bundle = infer_submission_methods(
            has_form=False, submission_email=None, texts=["Welcome."])
        self.assertEqual(bundle["methods"], [])
        self.assertTrue(bundle["reasons"])


# ---------------------------------------------------------------------------
# H–J: parsing, scoring, schema
# ---------------------------------------------------------------------------

class TestHFormParsing(unittest.TestCase):
    def test_relative_action_resolved(self):
        page = parsed(SUBMIT_URL, KZOW_SUBMIT)
        self.assertEqual(page.forms, ["https://kzow.example/submit-music"])

    def test_actionless_form_recorded(self):
        page = parsed("https://t.example/x",
                      "<html><body><form><input></form></body></html>")
        self.assertEqual(page.forms, [""])


class TestIContactScoring(unittest.TestCase):
    DOMAINS = {"kzow.example"}

    def test_strong_contact_scores_high_with_reasons(self):
        score, reasons = score_contact({
            "email": "music@kzow.example",
            "name": "Alex Rivera",
            "role": "music_director",
            "source_url": SUBMIT_URL,
        }, self.DOMAINS)
        self.assertLessEqual(score, 0.95)   # unverified contacts are capped
        self.assertGreaterEqual(score, 0.9)
        self.assertTrue(any("station domain" in r.lower() for r in reasons))
        self.assertTrue(any("named person" in r.lower() for r in reasons))

    def test_free_provider_penalized(self):
        weak, weak_reasons = score_contact({
            "email": "artist@gmail.com",
            "role": "unknown",
            "source_url": "https://kzow.example/about",
        }, self.DOMAINS)
        strong, _ = score_contact({
            "email": "music@kzow.example",
            "role": "unknown",
            "source_url": "https://kzow.example/about",
        }, self.DOMAINS)
        self.assertLess(weak, strong)
        self.assertTrue(any("free-provider" in r.lower() for r in weak_reasons))

    def test_score_stays_positive_floor(self):
        score, reasons = score_contact({"role": "unknown"}, set())
        self.assertGreaterEqual(score, 0.05)
        self.assertTrue(reasons)


class TestJSchemaShapes(unittest.TestCase):
    def test_enriched_contact_defaults_and_dict(self):
        contact = EnrichedContact(station_id="s1", role="host")
        data = contact.to_dict()
        expected_keys = {
            "id", "station_id", "name", "role", "email", "phone",
            "source_url", "confidence_score", "confidence_reasons",
            "verified_at", "preferred_for_submissions", "provenance",
        }
        self.assertEqual(set(data.keys()), expected_keys)
        self.assertFalse(data["preferred_for_submissions"])
        self.assertIsNone(data["verified_at"])

    def test_submission_path_roundtrip(self):
        path = SubmissionPath()
        path.methods = {"methods": ["email"], "kind": "inference"}
        data = path.to_dict()
        self.assertEqual(data["methods"]["methods"], ["email"])
        self.assertIsNone(data["submission_url"])

    def test_source_fetch_record_serializes(self):
        rec = SourceFetchRecord(url="https://x.example/",
                                ok=False, error_kind="timeout",
                                fetched_at="2026-08-21T00:00:00+00:00")
        data = rec.to_dict()
        self.assertEqual(data["error_kind"], "timeout")
        self.assertFalse(data["ok"])
        self.assertIsNone(data["status"])

    def test_radio_intelligence_record_defaults(self):
        record = RadioIntelligenceRecord()
        data = record.to_dict()
        self.assertEqual(data["organization_type"], "radio_station")
        self.assertEqual(data["status"], "enriched")


# ---------------------------------------------------------------------------
# K–O: intelligence assembly
# ---------------------------------------------------------------------------

class TestKOfflineMinimalBuild(unittest.TestCase):
    def test_minimal_station_offline(self):
        record = build_intelligence_record(
            {"id": "abc", "name": "Minimal FM", "website": "https://m.example/"},
            pages=[], fetch_records=None)
        self.assertEqual(record.name, "Minimal FM")
        self.assertEqual(record.status, "enriched")
        self.assertEqual(record.raw_metadata["enrichment_mode"],
                         "offline_facts_only")
        self.assertEqual(record.fetches, [])
        self.assertIsNone(record.submission)
        self.assertEqual(record.contacts, [])
        self.assertEqual(record.emails, [])


class TestLCharacteristicsCarryAndMerge(unittest.TestCase):
    def test_carried_classification_and_description_survive(self):
        station = kzow_station(
            station_type="commercial",
            classification_confidence=0.85,
            classification_evidence=["fcc keyword"],
            genres=["jazz"],
            format="music",
            description="Jazz for the valley.",
        )
        record = build_intelligence_record(station, pages=[], fetch_records=None)
        self.assertEqual(record.station_type, "commercial")
        self.assertEqual(record.classification_evidence, ["fcc keyword"])
        self.assertEqual(record.description, "Jazz for the valley.")
        self.assertEqual(record.genres, ["jazz"])   # carried facts survive offline

    def test_pages_add_genres_formats_market(self):
        record = build_intelligence_record(kzow_station(),
                                           [parsed(HOME_URL, KZOW_HOME)],
                                           fetch_records=None)
        self.assertIn("jazz", record.genres)
        self.assertIn("blues", record.genres)
        self.assertIn("jazz", record.genre_evidence)
        self.assertIn("music", record.formats)
        self.assertEqual(record.market_area, "Grand Valley")


class TestMContactRebuildFromPages(unittest.TestCase):
    def test_staff_page_yields_named_md_and_role_contacts(self):
        station = kzow_station()
        record = build_intelligence_record(
            station,
            [parsed(STAFF_URL, KZOW_STAFF)],
            fetch_records=None)
        by_email = {c.email: c for c in record.contacts}
        md = by_email.get("music@kzow.example")
        self.assertIsNotNone(md)
        self.assertEqual(md.role, "music_director")
        self.assertEqual(md.name, "Alex Rivera")
        self.assertEqual(by_email["bookings@kzow.example"].role, "booking")
        self.assertEqual(by_email["press@kzow.example"].role, "media")
        self.assertGreater(md.confidence_score, 0.5)
        self.assertTrue(md.confidence_reasons)

    def test_existing_contact_provenance_preserved_and_extended(self):
        station = kzow_station(contacts=[{
            "id": "c-1", "station_id": station_id_of(kzow_station()),
            "name": None, "role": "unknown",
            "email": "music@kzow.example", "phone": None,
            "source_url": STAFF_URL, "confidence_score": 0.3,
            "verified_at": None,
            "provenance": [{"value": "legacy-marker"}],
        }])
        record = build_intelligence_record(
            station, [parsed(STAFF_URL, KZOW_STAFF)], fetch_records=None)
        md = next(c for c in record.contacts if c.email == "music@kzow.example")
        self.assertEqual(md.id, "c-1")
        self.assertIn({"value": "legacy-marker"}, md.provenance)
        self.assertGreaterEqual(len(md.provenance), 2)


def station_id_of(station: dict) -> str:
    return station["id"]


class TestNPreferredPromotionRules(unittest.TestCase):
    def test_generic_inbox_never_promoted(self):
        station = kzow_station(submission_url=None, contact_url=None,
                               contacts=[{
                                   "id": "g1",
                                   "station_id": station_id_of(kzow_station()),
                                   "name": None, "role": "general",
                                   "email": "info@kzow.example", "phone": None,
                                   "source_url": HOME_URL,
                                   "confidence_score": 0.3,
                                   "verified_at": None, "provenance": [],
                               }])
        record = build_intelligence_record(station, pages=[], fetch_records=None)
        self.assertEqual(len(record.contacts), 1)
        self.assertFalse(record.contacts[0].preferred_for_submissions)
        self.assertIsNone(record.submission)
        self.assertIsNone(record.submission)

    def test_music_director_preferred_when_evidenced(self):
        record = build_intelligence_record(
            kzow_station(), [parsed(STAFF_URL, KZOW_STAFF)], fetch_records=None)
        preferred = [c for c in record.contacts if c.preferred_for_submissions]
        self.assertEqual(len(preferred), 1)
        self.assertEqual(preferred[0].role, "music_director")


class TestOSubmissionPathAssembly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = build_intelligence_record(
            kzow_station(),
            [parsed(SUBMIT_URL, KZOW_SUBMIT),
             parsed(STAFF_URL, KZOW_STAFF),
             parsed(HOME_URL, KZOW_HOME)],
            fetch_records=None)

    def test_path_fully_assembled(self):
        path = self.record.submission
        self.assertIsNotNone(path)
        self.assertEqual(path.submission_url["value"], SUBMIT_URL)
        self.assertEqual(path.programming_contact_role, "music_director")
        self.assertEqual(path.submission_email, "music@kzow.example")
        self.assertIn("accepts music submissions", path.instructions["value"])
        self.assertEqual(path.instructions["source_url"], SUBMIT_URL)
        tokens = {r["restriction"] for r in path.restrictions}
        self.assertGreaterEqual(len(tokens), 3)
        self.assertIn("no_attachments", tokens)
        self.assertIn("web_form", path.methods["methods"])
        self.assertIn("email", path.methods["methods"])
        self.assertEqual(path.methods["kind"], "inference")
        self.assertGreaterEqual(path.confidence_score, 0.85)
        self.assertLessEqual(path.confidence_score, 0.95)

    def test_overall_confidence_deltas_explained(self):
        record = self.record
        self.assertAlmostEqual(record.confidence_score, 0.80)
        joined = " ".join(record.confidence_reasons)
        self.assertIn("genre evidence", joined)
        self.assertIn("submission instructions", joined)
        self.assertEqual(record.status, "enriched")

    def test_discovery_fact_wins_for_submission_url(self):
        custom = kzow_station(submission_url={
            "value": SUBMIT_URL, "source_url": HOME_URL,
            "source_type": "link_anchor_rule", "method": "anchor_rule",
            "discovered_at": "", "also_seen_at": [],
        })
        record = build_intelligence_record(custom, pages=[], fetch_records=None)
        self.assertEqual(record.submission.submission_url["value"], SUBMIT_URL)
        self.assertEqual(record.submission.confidence_reasons,
                         ["dedicated submission URL discovered"])


# ---------------------------------------------------------------------------
# P–T: engine, isolation, CLI
# ---------------------------------------------------------------------------

class TestPEnrichmentEngineOffline(unittest.TestCase):
    def test_offline_engine_makes_no_fetches(self):
        engine = EnrichmentEngine()   # no fetcher injected
        self.assertFalse(engine.live)
        stations = [kzow_station(name="A FM", id="a"),
                    kzow_station(name="B FM", id="b")]
        result = engine.enrich_records(stations)
        self.assertEqual(result.record_count, 2)
        self.assertEqual(result.failure_count, 0)
        for record in result.records:
            self.assertEqual(record["fetches"], [])
            self.assertEqual(record["raw_metadata"]["enrichment_mode"],
                             "offline_facts_only")
            self.assertEqual(record["status"], "enriched")


class TestQEnrichmentEngineWithFetcher(unittest.TestCase):
    def test_fetch_targets_prioritized_and_parsed(self):
        engine, fetcher = make_engine()
        result = engine.enrich_records([kzow_station()])
        record = result.records[0]
        # Priority: submission > contact > homepage.
        self.assertEqual(fetcher.fetched[:3],
                         [SUBMIT_URL, STAFF_URL, HOME_URL])
        self.assertTrue(all(f["ok"] for f in record["fetches"]))
        self.assertEqual(record["raw_metadata"]["enrichment_mode"], "pages")
        self.assertIn("jazz", record["genres"])
        self.assertIsNotNone(record["submission"])
        # Existing email fact survived; new one added with provenance.
        values = {e["value"] for e in record["emails"]}
        self.assertIn("info@kzow.example", values)
        self.assertIn("music@kzow.example", values)
        music_fact = next(e for e in record["emails"]
                          if e["value"] == "music@kzow.example")
        self.assertIn(STAFF_URL, music_fact["also_seen_at"])

    def test_failed_fetch_isolated_per_url(self):
        pages = {STAFF_URL: KZOW_STAFF}
        engine, _ = make_engine(pages)
        result = engine.enrich_records([kzow_station()])
        record = result.records[0]
        kinds = {f["error_kind"] for f in record["fetches"]}
        self.assertIn("dns_error", kinds)
        self.assertEqual(result.failure_count, 0)   # URL miss ≠ station failure


class TestRPerStationIsolation(unittest.TestCase):
    def test_poisoned_station_does_not_kill_run(self):
        class Poison:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        engine = EnrichmentEngine()
        result = engine.enrich_records([Poison(), kzow_station()])
        self.assertEqual(result.failure_count, 1)
        failure = result.failures[0].to_dict()
        self.assertEqual(failure["stage"], "enrichment")
        self.assertEqual(failure["error_kind"], "RuntimeError")
        self.assertIn("boom", failure["message"])
        self.assertEqual(result.record_count, 1)
        self.assertEqual(result.records[0]["name"], "KZOW 98.3 FM")


class TestSEnrichmentResultModel(unittest.TestCase):
    def test_counts_and_serialization(self):
        result = EnrichmentResult()
        self.assertEqual(result.record_count, 0)
        self.assertEqual(result.failure_count, 0)
        data = result.to_dict()
        self.assertIn("started_at", data)
        self.assertIn("completed_at", data)
        json.dumps(data)   # must be JSON-mappable


class TestTCliMain(unittest.TestCase):
    def test_cli_offline_wrapper_and_bare_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "wrapper.json"
            wrapper.write_text(json.dumps({"records": [kzow_station()]}),
                               encoding="utf-8")
            out1 = Path(tmp) / "out1.json"
            self.assertEqual(main(["--input", str(wrapper),
                                   "--output", str(out1)]), 0)
            payload = json.loads(out1.read_text(encoding="utf-8"))
            self.assertEqual(payload["record_count"], 1)
            self.assertEqual(payload["records"][0]["name"], "KZOW 98.3 FM")
            self.assertEqual(payload["records"][0]["status"], "enriched")
            self.assertEqual(payload["failure_count"], 0)

            bare = Path(tmp) / "bare.json"
            bare.write_text(json.dumps([kzow_station(id="b2")]),
                            encoding="utf-8")
            out2 = Path(tmp) / "out2.json"
            self.assertEqual(main(["--input", str(bare),
                                   "--output", str(out2)]), 0)
            payload2 = json.loads(out2.read_text(encoding="utf-8"))
            self.assertEqual(payload2["record_count"], 1)
            self.assertEqual(payload2["records"][0]["submission"]
                             ["submission_url"]["value"], SUBMIT_URL)


if __name__ == "__main__":
    unittest.main()
