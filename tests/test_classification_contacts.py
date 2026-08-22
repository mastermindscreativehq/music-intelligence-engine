"""Phase 2 tests: station classification, socials, contact building."""

import unittest

from crawler.pages import parse_html
from enrichment.contacts import (
    build_contacts_from_page,
    extract_contact_names,
    extract_phone_numbers,
)
from enrichment.stations import (
    classify_station,
    detect_social_urls,
    page_role_from_url,
)


class TestStationClassification(unittest.TestCase):
    def test_college_evidence(self):
        result = classify_station([
            "WXYZ is the university radio station, campus radio since 1974.",
            "student radio collective",
        ])
        self.assertIn(result.station_type, ("college", "university", "campus"))
        self.assertGreaterEqual(result.confidence, 0.4)
        self.assertTrue(result.evidence)

    def test_community_evidence(self):
        result = classify_station(
            ["Community radio for the valley", "listener-supported"])
        self.assertEqual(result.station_type, "community")

    def test_unknown_when_no_evidence(self):
        result = classify_station(["We play songs sometimes."])
        self.assertEqual(result.station_type, "unknown")
        self.assertEqual(result.confidence, 0.1)
        self.assertEqual(result.evidence, [])

    def test_empty_texts(self):
        result = classify_station([])
        self.assertEqual(result.station_type, "unknown")


class TestSocialDetection(unittest.TestCase):
    def test_platforms_detected(self):
        links = [
            "https://www.facebook.com/stationpage",
            "https://www.instagram.com/stationpage/",
            "https://x.com/stationpage",
            "https://www.youtube.com/@stationpage",
            "https://www.linkedin.com/company/stationpage/",
        ]
        socials = detect_social_urls(links)
        self.assertEqual(set(socials), {"facebook", "instagram", "x",
                                        "youtube", "linkedin"})

    def test_non_social_ignored(self):
        self.assertEqual(detect_social_urls(["https://station.example/x"]), {})


class TestPageRoleMarkers(unittest.TestCase):
    def test_markers(self):
        self.assertEqual(page_role_from_url("https://x.example/contact-us"),
                         "contact_page")
        self.assertEqual(page_role_from_url("https://x.example/submissions"),
                         "submission_page")
        self.assertIsNone(page_role_from_url("https://x.example/"))


class TestContactBuilding(unittest.TestCase):
    def test_phone_extraction_and_validation(self):
        phones = extract_phone_numbers(
            "Call (555) 123-4567 or 555.999.0000; not 12345.")
        self.assertIn("(555) 123-4567", phones)
        self.assertIn("(555) 999-0000", phones)

    def test_invalid_phone_rejected(self):
        self.assertNotIn("(100) 200-3000",
                         extract_phone_numbers("(100) 200-3000"))

    def test_name_adjacency_patterns(self):
        pairs = extract_contact_names(
            "Music Director: Jane Smith runs submissions.\n"
            "Bob Rivera, Station Manager said hello.")
        by_name = dict(pairs)
        self.assertEqual(by_name.get("Jane Smith"), "music_director")
        self.assertEqual(by_name.get("Bob Rivera"), "station_manager")

    def test_allcaps_names_rejected(self):
        pairs = extract_contact_names("Music Director: STATION STAFF TEAM")
        self.assertEqual(pairs, [])

    def test_build_contacts_from_fixture_page(self):
        html = (
            "<html><body><p>Music Director: Jane Smith</p>"
            "<a href='mailto:music@kqxr.example?subject=Hi'>mail</a>"
            "<p>Call (555) 123-4567.</p></body></html>"
        )
        page = parse_html("https://kqxr.example/submissions", html)
        contacts = build_contacts_from_page(page)
        emails = {c["email"] for c in contacts}
        self.assertIn("music@kqxr.example", emails)
        jane = next(c for c in contacts
                    if c["email"] == "music@kqxr.example")
        self.assertEqual(jane["role"], "music_director")
        self.assertEqual(jane["name"], "Jane Smith")
        # Provenance entries exist and carry a source URL.
        self.assertTrue(all(p["source_url"] for p in jane["provenance"]))


if __name__ == "__main__":
    unittest.main()
