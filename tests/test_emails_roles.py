"""Phase 2 tests: email extraction, normalization, quality, roles."""

import unittest

from enrichment.emails import (
    email_quality,
    extract_emails_from_text,
    extract_mailto_addresses,
    normalize_email,
)
from enrichment.roles import classify_email_context, classify_role


class TestEmailNormalization(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(normalize_email("  Info@Station.Example. "),
                         "info@station.example")

    def test_surrounding_punctuation_stripped(self):
        self.assertEqual(normalize_email("<music@station.example>,"),
                         "music@station.example")

    def test_internal_whitespace_collapsed(self):
        self.assertEqual(normalize_email("music @ station.example"),
                         "music@station.example")

    def test_invisible_characters_removed(self):
        self.assertEqual(
            normalize_email("in\u200bfo@sta\u00a0tion.example"),
            "info@station.example")

    def test_invalid_shapes_return_none(self):
        for bad in ("not-an-email", "a@@b.example", ".lead@x.example",
                    "trail.@x.example", "double..dot@x.example",
                    "user@domain", "", None):
            with self.subTest(value=bad):
                self.assertIsNone(normalize_email(bad))


class TestEmailExtraction(unittest.TestCase):
    def test_text_extraction_ordered_unique(self):
        text = "Reach music@st.example or info@st.example; again " \
               "MUSIC@ST.EXAMPLE."
        self.assertEqual(extract_emails_from_text(text),
                         ["music@st.example", "info@st.example"])

    def test_mailto_with_query_string(self):
        out = extract_mailto_addresses(
            ["music@st.example?subject=Hello%20There"])
        self.assertEqual(out, ["music@st.example"])

    def test_obfuscation_decoded(self):
        # Common [at]/[dot] obfuscation is decoded deterministically.
        text = "email us at contact [at] station [dot] example"
        self.assertEqual(extract_emails_from_text(text),
                         ["contact@station.example"])

    def test_malformed_candidates_dropped(self):
        text = "bad@@st.example and trailing.@st.example but ok@st.example"
        self.assertEqual(extract_emails_from_text(text), ["ok@st.example"])


class TestEmailQuality(unittest.TestCase):
    def test_own_domain_role_inbox_is_professional(self):
        quality = email_quality("music@kqxr.example", {"kqxr.example"})
        self.assertEqual(quality["tier"], "professional")
        self.assertTrue(quality["matches_station_domain"])
        self.assertIn("role_inbox", quality["signals"])

    def test_free_provider_flagged(self):
        quality = email_quality("someone@gmail.com", {"kqxr.example"})
        self.assertEqual(quality["tier"], "weak")
        self.assertIn("free_provider", quality["signals"])

    def test_never_claims_deliverability(self):
        quality = email_quality("music@kqxr.example", {"kqxr.example"})
        blob = str(quality).lower()
        self.assertNotIn("deliverable", blob)
        self.assertNotIn("verified", blob)


class TestRoleClassification(unittest.TestCase):
    def test_documented_mappings(self):
        cases = [
            ("Contact the music director", "music_director"),
            ("Our Program Director reviews demos", "program_director"),
            ("Programming Director team", "programming"),
            ("To submit music, email us", "music_submission"),
            ("Resident DJ nights", "dj"),
            ("The station manager office", "station_manager"),
            ("For general inquiries write to", "general"),
            ("Advertising and underwriting", "advertising"),
            ("Something entirely else", "unknown"),
            ("", "unknown"),
            (None, "unknown"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_role(text), expected)

    def test_music_director_wins_over_generic_director_rules(self):
        self.assertEqual(classify_role("assistant music director"),
                         "music_director")

    def test_context_classifier_delegates(self):
        self.assertEqual(
            classify_email_context("questions? general inquiries desk"),
            "general")


if __name__ == "__main__":
    unittest.main()
