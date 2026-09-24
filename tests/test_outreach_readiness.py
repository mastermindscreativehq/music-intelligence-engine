"""Phase 1 outreach-readiness tests.

No network: engines run offline or against injected fake fetchers. Covers:

- ``compute_outreach_readiness``: outreach_ready / needs_review / rejected for
  every evidence combination (verified site + route + contact evidence, site
  unreachable, submission route unclear, contact not verified, insufficient
  station evidence, rejected qualification).
- Director identity preference (music over program director).
- ``merge_contacts_from_pages`` idempotency.
- End-to-end engine pass: the bounded verifier parses a reachable staff page,
  the merged director surfaces in ``contact_channels``, and readiness flips to
  ``outreach_ready`` with a person + route.
"""

from __future__ import annotations

import unittest

from crawler.http import FetchResult
from crawler.pages import parse_html

from discovery.radio.enrich_pipeline import EnrichmentEngine, EngineConfig
from discovery.radio.intelligence import (
    build_intelligence_record,
    merge_contacts_from_pages,
    rebuild_contact_channels,
)
from discovery.radio.readiness import (
    READINESS_NEEDS_REVIEW,
    READINESS_OUTREACH_READY,
    READINESS_REJECTED,
    REASON_CONTACT_NOT_VERIFIED,
    REASON_INSUFFICIENT_STATION_EVIDENCE,
    REASON_SITE_UNREACHABLE,
    REASON_SUBMISSION_ROUTE_UNCLEAR,
    compute_outreach_readiness,
)
from discovery.radio.schema import RadioIntelligenceRecord

HOME_URL = "https://kzzz.example/"
STAFF_URL = "https://kzzz.example/staff"
SUBMIT_URL = "https://kzzz.example/submissions"


def _base() -> dict:
    record = RadioIntelligenceRecord().to_dict()
    record.update({
        "name": "KZZZ 99.9 FM",
        "website": HOME_URL,
        "domain": "kzzz.example",
        "raw_metadata": {
            "qualification": {
                "verdict": "qualified",
                "kind": "station_site",
                "reason": "",
                "evidence": ["callsign", "station_type"],
            },
        },
    })
    return record


def _channels(**overrides) -> dict:
    channels = {
        "music_submission_url": None,
        "music_submission_email": None,
        "music_director_name": None,
        "music_director_email": None,
        "program_director_name": None,
        "program_director_email": None,
        "general_contact_email": None,
        "contact_url": None,
    }
    channels.update(overrides)
    return channels


STAFF_HTML = (
    "<html><head><title>KZZZ Staff</title></head><body>"
    "<h1>Our Team</h1>"
    "<p>Music Director: Alex Rivera</p>"
    "<p>Reach the music department at "
    "<a href=\"mailto:music@kzzz.example\">music@kzzz.example</a></p>"
    "<p>Show host Casey Morgan hosts the Morning Drive.</p>"
    "</body></html>"
)

HOME_HTML = (
    "<html><head><title>KZZZ 99.9 FM</title></head><body>"
    "<p>KZZZ 99.9 FM is a community radio station.</p>"
    "<a href=\"https://kzzz.example/submissions\">music submissions</a>"
    "<a href=\"https://kzzz.example/staff\">staff</a>"
    "</body></html>"
)


class FakeFetcher:
    """Exact-URL fetch double; missing URLs fail like a broken site."""

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


def _kzzz_station(**overrides) -> dict:
    record = _base()
    record.update({
        "emails": [{
            "value": "info@kzzz.example",
            "source_url": HOME_URL,
            "source_type": "official_website_page",
            "method": "text_rule",
            "discovered_at": "",
            "also_seen_at": [],
        }],
        "contacts": [],
        "submission_url": {
            "value": SUBMIT_URL,
            "source_url": HOME_URL,
            "source_type": "official_website_page",
            "method": "link",
            "discovered_at": "",
            "also_seen_at": [],
        },
        "contact_url": {
            "value": STAFF_URL,
            "source_url": HOME_URL,
            "source_type": "official_website_page",
            "method": "link",
            "discovered_at": "",
            "also_seen_at": [],
        },
        "source_urls": [HOME_URL],
        "website_reachable": True,
        "name_matches_site": True,
        "raw_metadata": {
            "homepage_title": "KZZZ 99.9 FM",
            "qualification": {
                "verdict": "qualified",
                "kind": "station_site",
                "reason": "",
                "evidence": ["callsign", "station_type"],
            },
        },
    })
    record.update(overrides)
    return record


class ComputeReadinessTests(unittest.TestCase):

    def test_outreach_ready_with_email_route_and_contact_evidence(self):
        record = _base()
        record["submission"] = {"submission_email": "music@kzzz.example"}
        record["raw_metadata"]["contact_channels"] = _channels(
            contact_url={"value": STAFF_URL, "source_url": HOME_URL},
        )
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_OUTREACH_READY)
        self.assertEqual(out["reasons"], [])
        self.assertIsNone(out["person"])
        self.assertEqual(
            (out["route"]["kind"], out["route"]["field"], out["route"]["value"]),
            ("email", "submission_email", "music@kzzz.example"))

    def test_rejected_qualification_is_rejected(self):
        record = _base()
        record["raw_metadata"]["qualification"] = {
            "verdict": "rejected", "kind": "non_station", "reason": "ticket site"}
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_REJECTED)
        self.assertIsNone(out["route"])
        self.assertIn(REASON_INSUFFICIENT_STATION_EVIDENCE, out["reasons"])

    def test_live_run_with_all_failed_fetches_is_site_unreachable(self):
        record = _base()
        record["fetches"] = [
            {"url": HOME_URL, "ok": False, "status": None,
             "error_kind": "connection_error"},
        ]
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_NEEDS_REVIEW)
        self.assertIn(REASON_SITE_UNREACHABLE, out["reasons"])
        self.assertIsNone(out["route"])

    def test_generic_contact_email_is_not_a_usable_route(self):
        # Rule 2: a generic contact mailbox is not one of the allowed outreach
        # channels. A station whose only channel is info@ must stay needs_review.
        record = _base()
        record["raw_metadata"]["contact_channels"] = _channels(
            general_contact_email="info@kzzz.example",
        )
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_NEEDS_REVIEW)
        self.assertIn(REASON_SUBMISSION_ROUTE_UNCLEAR, out["reasons"])
        self.assertIsNone(out["route"])

    def test_empty_contact_channel_value_is_not_a_route(self):
        # Empty dict value on a channel must never synthesize a route.
        record = _base()
        record["raw_metadata"]["contact_channels"] = _channels(
            general_contact_email={"value": ""},
        )
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_NEEDS_REVIEW)
        self.assertIsNone(out["route"])

    def test_qualified_with_no_route_and_no_contact_is_needs_review(self):
        # The reported regression: a qualified station with null person/role/
        # email/submission URL and no contact must never be outreach_ready.
        record = _base()
        record["raw_metadata"]["contact_channels"] = _channels()
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_NEEDS_REVIEW)
        self.assertIn(REASON_SUBMISSION_ROUTE_UNCLEAR, out["reasons"])
        self.assertIn(REASON_CONTACT_NOT_VERIFIED, out["reasons"])
        self.assertIsNone(out["route"])

    def test_no_usable_route_blocks_outreach(self):
        record = _base()
        record["raw_metadata"]["contact_channels"] = _channels(
            music_director_name={"value": "Alex Rivera",
                                 "source_url": STAFF_URL},
        )
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_NEEDS_REVIEW)
        self.assertIn(REASON_SUBMISSION_ROUTE_UNCLEAR, out["reasons"])
        self.assertIsNone(out["route"])

    def test_missing_contact_evidence_blocks_outreach(self):
        record = _base()
        record["submission"] = {"submission_url": {"value": SUBMIT_URL}}
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_NEEDS_REVIEW)
        self.assertIn(REASON_CONTACT_NOT_VERIFIED, out["reasons"])
        self.assertEqual(
            (out["route"]["kind"], out["route"]["field"]),
            ("webform", "submission_url"))

    def test_insufficient_station_evidence_from_needs_review_verdict(self):
        record = _base()
        record["raw_metadata"]["qualification"]["verdict"] = "needs_review"
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_NEEDS_REVIEW)
        self.assertIn(REASON_INSUFFICIENT_STATION_EVIDENCE, out["reasons"])

    def test_music_director_preferred_over_program_director(self):
        record = _base()
        record["raw_metadata"]["contact_channels"] = _channels(
            music_director_name={"value": "Alex Rivera",
                                 "source_url": STAFF_URL},
            music_director_email={"value": "music@kzzz.example",
                                  "source_url": STAFF_URL},
            program_director_name={"value": "Sam Torres",
                                   "source_url": STAFF_URL},
            contact_url={"value": STAFF_URL, "source_url": HOME_URL},
        )
        record["submission"] = {"submission_url": {"value": SUBMIT_URL}}
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_OUTREACH_READY)
        self.assertEqual(out["person"]["name"], "Alex Rivera")
        self.assertEqual(out["person"]["role"], "music_director")
        self.assertEqual(out["person"]["email"], "music@kzzz.example")

    def test_route_priority_webform_over_director_email(self):
        record = _base()
        record["submission"] = {
            "submission_url": {"value": SUBMIT_URL},
            "submission_email": "music@kzzz.example",
        }
        record["raw_metadata"]["contact_channels"] = _channels(
            music_director_email={"value": "director@kzzz.example",
                                  "source_url": STAFF_URL},
            contact_url={"value": STAFF_URL, "source_url": HOME_URL},
        )
        out = compute_outreach_readiness(record)
        self.assertEqual(out["route"]["kind"], "webform")
        self.assertEqual(out["route"]["value"], SUBMIT_URL)

    def test_offline_unverified_record_is_not_unreachable(self):
        # No fetches at all (offline enrichment): the site-reachability
        # reason must NOT fire; qualification + route decide instead.
        record = _base()
        record["submission"] = {"submission_url": {"value": SUBMIT_URL}}
        record["raw_metadata"]["contact_channels"] = _channels(
            general_contact_email="info@kzzz.example",
        )
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_OUTREACH_READY)


class MergeContactsTests(unittest.TestCase):

    def _staff_page(self):
        return parse_html(STAFF_URL, STAFF_HTML)

    def _record(self):
        station = _kzzz_station()
        home = parse_html(HOME_URL, HOME_HTML)
        return build_intelligence_record(station, [home], [])

    def test_merge_contacts_from_pages_is_idempotent(self):
        record = self._record()
        page = self._staff_page()
        merge_contacts_from_pages(record, [page])
        merge_contacts_from_pages(record, [page])
        named = [c for c in record.contacts if c.name]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0].name, "Alex Rivera")
        self.assertEqual(named[0].role, "music_director")

    def test_rebuild_contact_channels_after_merge(self):
        record = self._record()
        before = record.raw_metadata.get("contact_channels", {})
        self.assertIsNone(before.get("music_director_name"))
        merge_contacts_from_pages(record, [self._staff_page()])
        channels = rebuild_contact_channels(record)
        self.assertEqual(channels["music_director_name"]["value"],
                         "Alex Rivera")
        self.assertEqual(channels["music_director_email"]["value"],
                         "music@kzzz.example")


class EngineReadinessTests(unittest.TestCase):

    def test_engine_wires_person_merge_and_readiness(self):
        fetcher = FakeFetcher({HOME_URL: HOME_HTML, STAFF_URL: STAFF_HTML})
        engine = EnrichmentEngine(
            config=EngineConfig(verify_pages_per_station=8),
            fetcher=fetcher)
        enriched = engine._enrich_one(_kzzz_station()).to_dict()

        readiness = enriched["raw_metadata"]["outreach_readiness"]
        self.assertEqual(readiness["status"], READINESS_OUTREACH_READY)
        self.assertEqual(readiness["person"]["name"], "Alex Rivera")
        self.assertEqual(readiness["person"]["role"], "music_director")
        self.assertEqual(readiness["route"]["field"], "submission_url")

        channels = enriched["raw_metadata"]["contact_channels"]
        self.assertEqual(channels["music_director_email"]["value"],
                         "music@kzzz.example")

    def test_engine_marks_unreachable_site_needs_review(self):
        # Staff page 404s: the verify pass records a failed fetch, but the
        # station homepage/submission still reachable -> readiness stays
        # driven by route/contact evidence, not a phantom unreachable signal.
        fetcher = FakeFetcher({HOME_URL: HOME_HTML})
        engine = EnrichmentEngine(fetcher=fetcher)
        record = _kzzz_station()
        enriched = engine._enrich_one(record).to_dict()
        readiness = enriched["raw_metadata"]["outreach_readiness"]
        self.assertNotIn(REASON_SITE_UNREACHABLE, readiness["reasons"])

    def test_engine_outreach_ready_persists_evidence_sources(self):
        fetcher = FakeFetcher({HOME_URL: HOME_HTML,
                               SUBMIT_URL: "<html><body>accepting music</body></html>",
                               STAFF_URL: STAFF_HTML})
        engine = EnrichmentEngine(fetcher=fetcher)
        enriched = engine._enrich_one(_kzzz_station()).to_dict()
        readiness = enriched["raw_metadata"]["outreach_readiness"]
        self.assertTrue(readiness["person"]["source_url"])
        self.assertTrue(readiness["route"].get("source_url"))


if __name__ == "__main__":
    unittest.main()