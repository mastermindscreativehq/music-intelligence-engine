"""Phase 4A tests: evidence-based role attribution near contacts.

The helper must only ever attribute a role when an explicit role label
from ROLE_RULES appears on the anchor's own line or within the asymmetric
line window (labels above win). Every attribution carries traceable
evidence: the exact matched label substring, its absolute character span
in the source text, and its line position relative to the anchor.
"""

import unittest

from enrichment.roles import (
    classify_role_near,
    classify_role_near_with_evidence,
    find_role_evidence_near,
)


class TestExplicitAttribution(unittest.TestCase):
    def test_music_director_label_above_address(self):
        text = "Music Director: Jane Doe\njane@station.example"
        anchor = text.index("jane@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "music_director")
        self.assertEqual(evidence["matched_label"], "Music Director")
        self.assertEqual(evidence["line_offset"], -1)
        self.assertEqual(
            text[evidence["char_start"]:evidence["char_end"]],
            "Music Director")

    def test_program_director_label_on_same_line(self):
        text = "Program Director: pd@station.example"
        anchor = text.index("pd@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "program_director")
        self.assertEqual(evidence["matched_label"], "Program Director")
        self.assertEqual(evidence["line_offset"], 0)
        self.assertEqual(evidence["line_index"], 0)

    def test_dj_label_supported_by_vocabulary(self):
        text = "Requests go to the DJ desk\nstudio@station.example"
        anchor = text.index("studio@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "dj")
        self.assertEqual(evidence["matched_label"], "DJ")

    def test_host_label_supported_by_vocabulary(self):
        text = "Our On-Air Host reads mail here:\njane@station.example"
        anchor = text.index("jane@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "host")
        self.assertIn("Host", evidence["matched_label"])
        self.assertEqual(evidence["line_offset"], -1)

    def test_role_word_inside_address_counts_as_same_line_evidence(self):
        # The localpart itself is source text on the anchor's line; a
        # documented label there is still explicit evidence.
        text = "Reach the morning team:\nhost@station.example"
        anchor = text.index("host@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "host")
        self.assertEqual(evidence["line_offset"], 0)
        self.assertEqual(evidence["matched_label"], "host")

    def test_music_submission_label_three_lines_above(self):
        text = ("Music submissions are reviewed weekly\n"
                "\n\nprogramming@station.example")
        anchor = text.index("programming@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "music_submission")
        self.assertEqual(evidence["line_offset"], -3)


class TestNoInvention(unittest.TestCase):
    def test_unrelated_text_near_contact_stays_unknown(self):
        text = ("Now playing: great new music all night long\n"
                "mail@station.example")
        anchor = text.index("mail@station.example")
        self.assertEqual(classify_role_near_with_evidence(text, anchor),
                         ("unknown", None))
        self.assertIsNone(find_role_evidence_near(text, anchor))

    def test_empty_and_missing_evidence_return_unknown(self):
        for text, anchor in (
            ("", 0),
            (None, 0),
            ("   \n  \n", 0),
            ("no labels at all\nmail@x.example", 5),
            ("mail@x.example", 99),
            ("mail@x.example", -1),
        ):
            with self.subTest(text=text, anchor=anchor):
                self.assertEqual(
                    classify_role_near_with_evidence(text, anchor),
                    ("unknown", None))

    def test_bare_role_word_without_label_context_not_invented(self):
        # The word "director" alone is NOT in the vocabulary as a bare
        # token; only full documented labels count.
        text = "the director's cut screening\nfilm@station.example"
        anchor = text.index("film@station.example")
        self.assertIsNone(find_role_evidence_near(text, anchor))


class TestTraceability(unittest.TestCase):
    def test_exact_source_casing_is_preserved(self):
        text = "MUSIC DIRECTOR — Jane\njane@station.example"
        anchor = text.index("jane@station.example")
        _, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(evidence["matched_label"], "MUSIC DIRECTOR")
        self.assertEqual(
            text[evidence["char_start"]:evidence["char_end"]],
            evidence["matched_label"])

    def test_evidence_locations_are_absolute_and_consistent(self):
        text = "line zero\nMusic Director\nclassical@station.example"
        anchor = text.index("classical@station.example")
        _, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(evidence["role"], "music_director")
        self.assertEqual(evidence["line_index"], 1)
        self.assertEqual(evidence["line_offset"], -1)
        self.assertLessEqual(evidence["char_start"], anchor - 1)
        self.assertGreater(evidence["char_end"], evidence["char_start"])

    def test_priority_order_on_one_line_matches_rule_order(self):
        # ROLE_RULES priority decides which label wins on a single line —
        # music_director outranks program_director regardless of position.
        text = "Program Director and Music Director: both@station.example"
        anchor = text.index("both@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "music_director")
        self.assertEqual(evidence["matched_label"], "Music Director")


class TestLegacyEntryPointParity(unittest.TestCase):
    CORPUS = [
        "Music Director: Jane Doe\njane@station.example",
        "pd@station.example\nProgram Director",
        "Resident DJ nights\nstudio@station.example",
        "Now playing: great tunes\nmail@station.example",
        "",
        None,
        "On-Air Host\nhost@station.example",
    ]

    def test_classify_role_near_matches_wrapper_everywhere(self):
        for text in self.CORPUS:
            if not isinstance(text, str):
                anchors = [0]
            else:
                anchors = [0, len(text) // 2, max(len(text) - 1, 0)]
            for anchor in anchors:
                with self.subTest(text=text, anchor=anchor):
                    expected = classify_role_near(text, anchor)
                    role, _ = classify_role_near_with_evidence(text, anchor)
                    self.assertEqual(expected, role)

    def test_labels_above_outrank_labels_below(self):
        # Asymmetry contract: a label ABOVE the address beats one BELOW it.
        text = ("Programming Director\n"
                "md@station.example\n"
                "send your demos to the music department")
        anchor = text.index("md@station.example")
        role, evidence = classify_role_near_with_evidence(text, anchor)
        self.assertEqual(role, "programming")
        self.assertEqual(evidence["matched_label"], "Programming Director")


if __name__ == "__main__":
    unittest.main()
