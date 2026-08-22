"""Phase 2 tests: deduplication, confidence scoring, provenance."""

import unittest

from enrichment.confidence import score_station, rescore
from enrichment.dedupe import deduplicate_stations, identity_key, merge_stations


def station_record(**overrides):
    base = {
        "id": "r1",
        "name": "KQXR 101.5 FM",
        "alternate_names": [],
        "website": "https://kqxr.example/",
        "country": "United States",
        "state_or_region": "Cedar State",
        "city": None,
        "station_type": "unknown",
        "classification_confidence": 0.0,
        "classification_evidence": [],
        "emails": [],
        "contacts": [],
        "phone_numbers": [],
        "submission_url": None,
        "contact_url": None,
        "programming_url": None,
        "social_urls": {},
        "source_urls": ["https://kqxr.example/"],
        "website_reachable": True,
        "name_matches_site": True,
        "discovered_at": "2026-08-21T00:00:00+00:00",
        "last_observed_at": "2026-08-21T01:00:00+00:00",
        "confidence_score": 0.0,
        "confidence_reasons": [],
    }
    base.update(overrides)
    return base


class TestIdentityKeys(unittest.TestCase):
    def test_domain_key_ignores_www_and_scheme(self):
        key_a = identity_key(station_record(
            website="http://www.kqxr.example/"))
        key_b = identity_key(station_record(website="https://kqxr.example/x"))
        self.assertEqual(key_a, key_b)
        self.assertEqual(key_a[0], "domain")

    def test_namegeo_fallback_without_website(self):
        record = station_record(website=None)
        key = identity_key(record)
        self.assertEqual(key[0], "namegeo")
        self.assertIn("kqxr-101-5-fm", key[1])
        self.assertIn("cedar-state", key[1])

    def test_same_name_different_region_never_merges(self):
        key_tx = identity_key(station_record(
            website=None, name="Sunrise Radio",
            state_or_region="Texas"))
        key_ny = identity_key(station_record(
            website=None, name="Sunrise Radio",
            state_or_region="New York"))
        self.assertNotEqual(key_tx, key_ny)


class TestMerging(unittest.TestCase):
    def test_merge_unions_and_preserves_provenance(self):
        primary = station_record(emails=[{
            "value": "music@kqxr.example",
            "source_url": "https://kqxr.example/submissions",
            "source_type": "submission_page",
            "method": "text_rule",
            "discovered_at": "2026-08-21T00:00:00+00:00",
            "also_seen_at": [],
        }], social_urls={"facebook": "https://facebook.com/kqxr"})
        duplicate = station_record(
            id="r2",
            website="https://www.kqxr.example/contact",
            source_urls=["https://www.kqxr.example/contact"],
            alternate_names=["KQXR Community"],
            emails=[{
                "value": "music@kqxr.example",
                "source_url": "https://www.kqxr.example/contact",
                "source_type": "contact_page",
                "method": "text_rule",
                "discovered_at": "2026-08-21T02:00:00+00:00",
                "also_seen_at": [],
            }],
            social_urls={"instagram": "https://instagram.com/kqxr"},
        )
        merged = merge_stations(primary, duplicate)
        self.assertEqual(merged["alternate_names"], ["KQXR Community"])
        self.assertEqual(len(merged["emails"]), 1)
        fact = merged["emails"][0]
        self.assertEqual(fact["source_url"],
                         "https://kqxr.example/submissions")   # original kept
        self.assertIn("https://www.kqxr.example/contact",
                      fact["also_seen_at"])                    # later sighting
        self.assertEqual(set(merged["social_urls"]),
                         {"facebook", "instagram"})
        self.assertEqual(len(merged["source_urls"]), 2)

    def test_deduplicate_reports_count(self):
        records = [
            station_record(),
            station_record(id="dup", name="KQXR alias"),
            station_record(id="other", name="Other",
                           website="https://other.example/"),
        ]
        merged, removed = deduplicate_stations(records)
        self.assertEqual(len(merged), 2)
        self.assertEqual(removed, 1)


class TestConfidence(unittest.TestCase):
    def test_full_evidence_scores_high_with_reasons(self):
        record = station_record(
            emails=[{"value": "music@kqxr.example",
                     "quality": {"signals": ["own_domain", "role_inbox"]}}],
            submission_url={"value": "https://kqxr.example/submissions"},
            contact_url={"value": "https://kqxr.example/contact"},
            source_urls=["https://a.example/", "https://b.example/"],
            station_type="community")
        score, reasons = score_station(record)
        self.assertGreater(score, 0.8)
        self.assertLessEqual(score, 1.0)
        blob = "\n".join(reasons).lower()
        for expected in ("website reachable", "contact page",
                         "submission page"):
            with self.subTest(reason=expected):
                self.assertIn(expected, blob)

    def test_broken_site_penalized(self):
        score, _ = score_station(station_record(website_reachable=False))
        self.assertLess(score, 0.3)

    def test_score_clamped(self):
        score, _ = score_station({})
        self.assertGreaterEqual(score, 0.0)

    def test_rescore_writes_back(self):
        record = station_record()
        updated = rescore(record)
        self.assertIs(updated, record)
        self.assertGreater(record["confidence_score"], 0.0)
        self.assertTrue(record["confidence_reasons"])


if __name__ == "__main__":
    unittest.main()
