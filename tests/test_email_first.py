"""Tests for email-first discovery strategy.

Covers:
- Obfuscated email extraction
- Staff directory filtering (name-only entries without music role dropped)
- Navigation word rejection in name detection
- Page prioritization for email-rich pages
"""

import unittest

from enrichment.emails import (
    extract_emails_from_text,
    normalize_email,
    _decode_obfuscated,
)
from enrichment.staff_directory import (
    _looks_like_person_name,
    _build_entry,
    _NAVIGATION_WORDS,
    _MUSIC_RELEVANT_ROLES,
)
from crawler.page_finder import score_link


# ---------------------------------------------------------------------------
# Obfuscated email extraction
# ---------------------------------------------------------------------------

class TestObfuscatedEmailExtraction(unittest.TestCase):

    def test_standard_email_still_works(self):
        text = "Contact us at music@kzow.example for submissions."
        emails = extract_emails_from_text(text)
        self.assertEqual(emails, ["music@kzow.example"])

    def test_bracket_at_bracket_dot(self):
        text = "Email: john [at] kzow [dot] com"
        emails = extract_emails_from_text(text)
        self.assertEqual(emails, ["john@kzow.com"])

    def test_paren_at_paren_dot(self):
        text = "Reach us at jane (at) example (dot) org"
        emails = extract_emails_from_text(text)
        self.assertEqual(emails, ["jane@example.org"])

    def test_uppercase_AT_DOT(self):
        text = "Send to admin AT station DOT fm"
        emails = extract_emails_from_text(text)
        self.assertEqual(emails, ["admin@station.fm"])

    def test_mixed_obfuscation_and_standard(self):
        text = "Official: info@kzow.example or webmaster [at] kzow [dot] example"
        emails = extract_emails_from_text(text)
        self.assertIn("info@kzow.example", emails)
        self.assertIn("webmaster@kzow.example", emails)

    def test_obfuscated_with_hyphenated_domain(self):
        text = "Contact webmaster [at] my-station [dot] com"
        emails = extract_emails_from_text(text)
        self.assertEqual(emails, ["webmaster@my-station.com"])

    def test_no_false_positives_on_normal_text(self):
        text = "Welcome to our station. We play jazz and blues."
        emails = extract_emails_from_text(text)
        self.assertEqual(emails, [])

    def test_obfuscated_empty_string(self):
        self.assertEqual(extract_emails_from_text(""), [])


# ---------------------------------------------------------------------------
# _looks_like_person_name: navigation word rejection
# ---------------------------------------------------------------------------

class TestNavigationWordRejection(unittest.TestCase):

    def test_contact_us_rejected(self):
        self.assertFalse(_looks_like_person_name("Contact Us"))

    def test_donate_now_rejected(self):
        self.assertFalse(_looks_like_person_name("Donate Now"))

    def test_stream_help_rejected(self):
        self.assertFalse(_looks_like_person_name("Stream Help"))

    def test_record_fair_rejected(self):
        self.assertFalse(_looks_like_person_name("Record Fair"))

    def test_database_design_rejected(self):
        self.assertFalse(_looks_like_person_name("Database Design"))

    def test_information_services_rejected(self):
        self.assertFalse(_looks_like_person_name("Information Services Cadre"))

    def test_volunteer_page_rejected(self):
        self.assertFalse(_looks_like_person_name("Volunteer Page"))

    def test_privacy_policy_rejected(self):
        self.assertFalse(_looks_like_person_name("Privacy Policy"))

    def test_mailing_address_rejected(self):
        self.assertFalse(_looks_like_person_name("Mailing Address"))

    def test_on_air_phone_rejected(self):
        self.assertFalse(_looks_like_person_name("On Air Phone"))

    def test_jane_doe_passes(self):
        self.assertTrue(_looks_like_person_name("Jane Doe"))

    def test_alex_rivera_passes(self):
        self.assertTrue(_looks_like_person_name("Alex Rivera"))

    def test_bob_smith_passes(self):
        self.assertTrue(_looks_like_person_name("Bob Smith"))


# ---------------------------------------------------------------------------
# Staff directory entry filtering
# ---------------------------------------------------------------------------

class TestStaffEntryFiltering(unittest.TestCase):

    def test_name_only_with_music_role_kept(self):
        entry = _build_entry(
            name="Jane Doe",
            role_text="Music Director",
            email=None,
            phone=None,
            source_url="https://example.com/staff",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["role"], "music_director")
        self.assertIsNone(entry["email"])

    def test_name_only_with_dj_role_kept(self):
        entry = _build_entry(
            name="Bob Smith",
            role_text="DJ",
            email=None,
            phone=None,
            source_url="https://example.com/staff",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["role"], "dj")

    def test_name_only_with_unknown_role_dropped(self):
        entry = _build_entry(
            name="Random Person",
            role_text=None,
            email=None,
            phone=None,
            source_url="https://example.com/staff",
        )
        self.assertIsNone(entry)

    def test_email_without_name_kept(self):
        entry = _build_entry(
            name=None,
            role_text=None,
            email="music@example.com",
            phone=None,
            source_url="https://example.com/staff",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["email"], "music@example.com")

    def test_email_with_name_kept(self):
        entry = _build_entry(
            name="Jane Doe",
            role_text="Program Director",
            email="jane@example.com",
            phone=None,
            source_url="https://example.com/staff",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["email"], "jane@example.com")
        self.assertEqual(entry["name"], "Jane Doe")

    def test_phone_only_dropped(self):
        entry = _build_entry(
            name=None,
            role_text=None,
            email=None,
            phone="(555) 123-4567",
            source_url="https://example.com/staff",
        )
        self.assertIsNone(entry)

    def test_email_with_phone_kept(self):
        entry = _build_entry(
            name="Jane Doe",
            role_text="Music Director",
            email="jane@example.com",
            phone="(555) 123-4567",
            source_url="https://example.com/staff",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["email"], "jane@example.com")
        self.assertEqual(entry["phone"], "(555) 123-4567")


# ---------------------------------------------------------------------------
# Page prioritization
# ---------------------------------------------------------------------------

class TestPagePrioritization(unittest.TestCase):

    def test_submission_page_beats_staff(self):
        sub_w = score_link("https://kzow.example/submissions", "Submit Music")
        staff_w = score_link("https://kzow.example/staff", "Our Staff")
        self.assertGreater(sub_w, staff_w)

    def test_contact_page_beats_staff(self):
        contact_w = score_link("https://kzow.example/contact", "Contact Us")
        staff_w = score_link("https://kzow.example/staff", "Our Staff")
        self.assertGreater(contact_w, staff_w)

    def test_programming_page_beats_staff(self):
        prog_w = score_link("https://kzow.example/programming", "Programming")
        staff_w = score_link("https://kzow.example/staff", "Our Staff")
        self.assertGreater(prog_w, staff_w)

    def test_playlist_page_scored(self):
        w = score_link("https://kzow.example/playlist", "Playlist")
        self.assertGreaterEqual(w, 6)

    def test_music_page_scored(self):
        w = score_link("https://kzow.example/music", "Music")
        self.assertGreaterEqual(w, 5)

    def test_irrelevant_page_not_scored(self):
        w = score_link("https://kzow.example/privacy", "Privacy Policy")
        self.assertEqual(w, 0)


if __name__ == "__main__":
    unittest.main()
