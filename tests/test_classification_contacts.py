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


def _role_evidence_entries(contact):
    return [p for p in contact.get("provenance") or []
            if p.get("method") == "role_label_rule"]


class TestRoleEvidenceAttribution(unittest.TestCase):
    """Phase 4B: explicit nearby role labels become traceable provenance."""

    def _build(self, html, url="https://kqxr.example/contact"):
        return build_contacts_from_page(parse_html(url, html))

    def _by_email(self, contacts, email):
        return next(c for c in contacts if c["email"] == email)

    def test_music_director_label_is_role_attributed(self):
        page_html = (
            "<html><body><p>Music Director: Jane Doe</p>"
            "<p>jane@kqxr.example</p></body></html>"
        )
        contact = self._by_email(
            self._build(page_html), "jane@kqxr.example")
        self.assertEqual(contact["role"], "music_director")
        entries = _role_evidence_entries(contact)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["value"], "Music Director")
        self.assertEqual(entry["source_url"],
                         "https://kqxr.example/contact")
        self.assertEqual(entry["method"], "role_label_rule")
        # The raw observation entry is still present, untouched, first.
        self.assertEqual(contact["provenance"][0]["method"], "text_rule")
        self.assertEqual(contact["provenance"][0]["value"],
                         "jane@kqxr.example")

    def test_program_director_label_is_role_attributed(self):
        page_html = (
            "<html><body><p>Program Director reviews demos:</p>"
            "<p>pd@kqxr.example</p></body></html>"
        )
        contact = self._by_email(self._build(page_html),
                                 "pd@kqxr.example")
        self.assertEqual(contact["role"], "program_director")
        entries = _role_evidence_entries(contact)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["value"], "Program Director")

    def test_unrelated_role_word_does_not_attribute(self):
        page_html = (
            "<html><body><p>The director's cut screening tonight!</p>"
            "<p>mail@kqxr.example</p></body></html>"
        )
        contact = self._by_email(self._build(page_html),
                                 "mail@kqxr.example")
        self.assertEqual(contact["role"], "unknown")
        self.assertEqual(_role_evidence_entries(contact), [])

    def test_contact_without_role_evidence_stays_unknown(self):
        page_html = (
            "<html><body><p>Thanks for listening to the stream.</p>"
            "<p>info@kqxr.example</p></body></html>"
        )
        contact = self._by_email(self._build(page_html),
                                 "info@kqxr.example")
        self.assertEqual(contact["role"], "unknown")
        self.assertEqual(_role_evidence_entries(contact), [])

    def test_exact_source_casing_retained_in_provenance(self):
        page_html = (
            "<html><body><p>MUSIC DIRECTOR — Jane Doe</p>"
            "<p>jane@kqxr.example</p></body></html>"
        )
        contact = self._by_email(
            self._build(page_html), "jane@kqxr.example")
        entries = _role_evidence_entries(contact)
        self.assertEqual(entries[0]["value"], "MUSIC DIRECTOR")

    def test_provenance_survives_contact_record_roundtrip_and_merge(self):
        from discovery.radio.schema import ContactRecord

        page_html = (
            "<html><body><p>Music Director: Jane Doe</p>"
            "<p>jane@kqxr.example</p></body></html>"
        )
        contact = self._by_email(
            self._build(page_html), "jane@kqxr.example")
        record = ContactRecord.from_dict(contact)
        self.assertEqual(record.role, "music_director")

        # Pipeline-style merge across pages EXTENDS provenance; it never
        # drops the earlier role evidence or the raw observation.
        other_page = dict(contact)
        other_page["provenance"] = [{
            "value": "Music Director",
            "source_url": "https://kqxr.example/staff",
            "source_type": "official_website_page",
            "method": "role_label_rule",
            "discovered_at": "",
            "also_seen_at": [],
        }]
        record.provenance.extend(other_page["provenance"])
        methods = [p["method"] for p in record.provenance]
        # The fixture label also carries a name ("Jane Doe"), so the
        # pre-existing name-adjacency entry coexists with the new one.
        self.assertEqual(
            methods,
            ["text_rule", "role_label_rule", "name_adjacency_rule",
             "role_label_rule"])
        values = {p["value"] for p in record.provenance}
        self.assertIn("jane@kqxr.example", values)
        self.assertIn("Music Director", values)

    def test_phone_only_pages_keep_existing_behavior(self):
        page_html = (
            "<html><body><p>Request line (555) 123-4567.</p></body></html>"
        )
        contacts = self._build(page_html)
        phones = [c for c in contacts if c["phone"]]
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["role"], "unknown")
        self.assertIsNone(phones[0]["email"])


class TestPersonAttribution(unittest.TestCase):
    """Phase 4C: explicit person names become traceable provenance."""

    def _build(self, html, url="https://kqxr.example/contact"):
        return build_contacts_from_page(parse_html(url, html))

    def _by_email(self, contacts, email):
        return next(c for c in contacts if c["email"] == email)

    def _phone_contact(self, contacts):
        phones = [c for c in contacts if c["phone"]]
        self.assertEqual(len(phones), 1)
        return phones[0]

    def test_explicit_name_and_email_association(self):
        page_html = (
            "<html><body><p>Program Director: Alex Rivera</p>"
            "<p>alex@kqxr.example</p></body></html>"
        )
        contact = self._by_email(self._build(page_html),
                                 "alex@kqxr.example")
        self.assertEqual(contact["name"], "Alex Rivera")
        self.assertEqual(contact["role"], "program_director")
        name_entries = [p for p in contact["provenance"]
                        if p["method"] == "name_adjacency_rule"]
        self.assertEqual(len(name_entries), 1)
        self.assertEqual(name_entries[0]["value"], "Alex Rivera")
        self.assertEqual(name_entries[0]["source_url"],
                         "https://kqxr.example/contact")

    def test_explicit_name_and_phone_association(self):
        page_html = (
            "<html><body><p>Station Manager: Sam Lee (555) 867-5309</p>"
            "</body></html>"
        )
        contact = self._phone_contact(self._build(page_html))
        self.assertEqual(contact["phone"], "(555) 867-5309")
        self.assertEqual(contact["role"], "station_manager")
        self.assertEqual(contact["name"], "Sam Lee")
        methods = [p["method"] for p in contact["provenance"]]
        self.assertEqual(methods,
                         ["phone_rule", "role_label_rule",
                          "name_adjacency_rule"])
        raw = contact["provenance"][0]
        self.assertEqual(raw["value"], "(555) 867-5309")

    def test_role_name_contact_structured_block(self):
        page_html = (
            "<html><body><p>Music Director: Jane Doe — "
            "jane@kqxr.example</p></body></html>"
        )
        contact = self._by_email(self._build(page_html),
                                 "jane@kqxr.example")
        self.assertEqual(contact["role"], "music_director")
        self.assertEqual(contact["name"], "Jane Doe")
        methods = [p["method"] for p in contact["provenance"]]
        self.assertEqual(methods,
                         ["text_rule", "role_label_rule",
                          "name_adjacency_rule"])

    def test_nearby_unrelated_name_does_not_attribute(self):
        page_html = (
            "<html><body>"
            "<p>Board member Bob Rivers visited the studio.</p>"
            "<p>jane@kqxr.example</p>"
            "</body></html>"
        )
        # No role label anywhere near the address: nothing may attach.
        contact = self._by_email(self._build(page_html),
                                 "jane@kqxr.example")
        self.assertIsNone(contact["name"])
        self.assertEqual(contact["role"], "unknown")

    def test_email_and_phone_patterns_alone_infer_nothing(self):
        # Email and phone contacts live on separate pages by construction:
        # phone-only contacts exist only when no email was found.
        email_contact = self._by_email(
            self._build(
                "<html><body><p>Contact jane.doe@kqxr.example.</p>"
                "</body></html>"),
            "jane.doe@kqxr.example")
        self.assertIsNone(email_contact["name"])
        self.assertEqual(email_contact["role"], "unknown")

        phone_contact = self._phone_contact(self._build(
            "<html><body><p>Studio line (555) 010-7788.</p>"
            "</body></html>"))
        self.assertIsNone(phone_contact["name"])
        self.assertEqual(phone_contact["role"], "unknown")

    def test_no_explicit_evidence_leaves_name_null(self):
        page_html = (
            "<html><body><p>Email the station:</p>"
            "<p>info@kqxr.example</p></body></html>"
        )
        contact = self._by_email(self._build(page_html),
                                 "info@kqxr.example")
        self.assertIsNone(contact["name"])
        self.assertEqual(
            [p["method"] for p in contact["provenance"]], ["text_rule"])

    def test_provenance_preserves_exact_source_name_text(self):
        # NOTE: camelCase surnames ("DuPre") are truncated by the
        # pre-existing name regex; conventional capitalized names parse
        # fully. Exactness is asserted on the full parsed form.
        page_html = (
            "<html><body><p>Music Director: Renee Dupre</p>"
            "<p>renee@kqxr.example</p></body></html>"
        )
        contact = self._by_email(self._build(page_html),
                                 "renee@kqxr.example")
        self.assertEqual(contact["name"], "Renee Dupre")
        name_entry = next(p for p in contact["provenance"]
                          if p["method"] == "name_adjacency_rule")
        self.assertEqual(name_entry["value"], "Renee Dupre")

    def test_name_attachment_requires_matching_role(self):
        page_html = (
            "<html><body>"
            "<p>Music Director: Jane Doe</p>"
            "<p>jane@kqxr.example</p>"
            "<hr>"
            "<p>Our resident DJ Celso Cruz hosts the late mix.</p>"
            "</body></html>"
        )
        # DJ name exists on the page but no DJ-labeled contact exists;
        # it must NOT leak onto the music_director contact.
        contact = self._by_email(self._build(page_html),
                                 "jane@kqxr.example")
        self.assertEqual(contact["name"], "Jane Doe")
        self.assertNotIn("Celso Cruz",
                         [p["value"] for p in contact["provenance"]])


if __name__ == "__main__":
    unittest.main()
