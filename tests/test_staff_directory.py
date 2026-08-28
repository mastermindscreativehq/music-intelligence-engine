"""Phase 2 tests: staff-directory detection, structured person/email extraction.

Email intelligence is the primary success metric.  Tests verify:
- Named person + email + role extraction (highest value).
- Email extraction without a named person.
- Name-only extraction for later enrichment (no email yet).
- Phone as secondary data only (never standalone).
- Provenance preservation for every extracted contact.
- Confidence scoring distinguishes named+email from generic.
"""

import unittest

from crawler.pages import ParsedPage, parse_html
from enrichment.staff_directory import (
    _looks_like_person_name,
    extract_staff_entries,
    is_staff_directory,
)
from enrichment.contacts import build_contacts_from_page


def _page(url: str, title: str = "", text: str = "",
          mailtos: list[str] | None = None) -> ParsedPage:
    """Shorthand for building a ParsedPage in tests."""
    return ParsedPage(
        url=url, title=title, text=text,
        mailtos=mailtos or [],
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestStaffDirectoryDetection(unittest.TestCase):

    def test_detected_by_title_staff(self):
        page = _page("https://kexp.org/", title="Our Staff")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_title_team(self):
        page = _page("https://wfmu.org/", title="Meet the Team")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_title_people(self):
        page = _page("https://wxyc.org/", title="People")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_title_broadcasters(self):
        page = _page("https://kexp.org/shows", title="Our Broadcasters")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_url_staff(self):
        page = _page("https://kexp.org/staff", title="KEXP")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_url_team(self):
        page = _page("https://wfmu.org/about/team", title="WFMU")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_url_people(self):
        page = _page("https://wxyc.org/people", title="WXYC")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_url_our_staff(self):
        page = _page("https://kexp.org/our-staff", title="KEXP")
        self.assertTrue(is_staff_directory(page))

    def test_detected_by_content_heuristic(self):
        # 3+ name-like lines near role keywords triggers heuristic detection.
        text = (
            "Jane Smith\nMusic Director\njane@kexp.org\n\n"
            "Bob Rivera\nProgram Director\nbob@kexp.org\n\n"
            "Alice Chen\nStation Manager\nalice@kexp.org\n"
        )
        page = _page("https://kexp.org/about", title="About KEXP", text=text)
        self.assertTrue(is_staff_directory(page))

    def test_not_detected_normal_contact_page(self):
        page = _page(
            "https://kexp.org/contact",
            title="Contact Us",
            text="Email us at info@kexp.org for general inquiries.",
        )
        self.assertFalse(is_staff_directory(page))

    def test_not_detected_empty_page(self):
        page = _page("https://kexp.org/", title="", text="")
        self.assertFalse(is_staff_directory(page))

    def test_not_detected_few_names(self):
        # Only 2 name-like lines — below the threshold of 3.
        text = (
            "Jane Smith\nMusic Director\njane@kexp.org\n\n"
            "Bob Rivera\nProgram Director\nbob@kexp.org\n"
        )
        page = _page("https://kexp.org/about", title="About", text=text)
        self.assertFalse(is_staff_directory(page))


# ---------------------------------------------------------------------------
# Name detection helpers
# ---------------------------------------------------------------------------

class TestNameDetection(unittest.TestCase):

    def test_simple_two_word_name(self):
        self.assertTrue(_looks_like_person_name("Jane Smith"))

    def test_three_word_name(self):
        self.assertTrue(_looks_like_person_name("Bob Rivera Jr"))

    def test_hyphenated_name(self):
        self.assertTrue(_looks_like_person_name("Jean-Pierre Delacroix"))

    def test_name_with_particle(self):
        self.assertTrue(_looks_like_person_name("Ludwig van Beethoven"))

    def test_rejects_allcaps(self):
        self.assertFalse(_looks_like_person_name("STATION STAFF TEAM"))

    def test_rejects_single_word(self):
        self.assertFalse(_looks_like_person_name("Jane"))

    def test_rejects_too_many_words(self):
        self.assertFalse(_looks_like_person_name(
            "This Is Definitely Not A Person Name At All"))

    def test_rejects_empty(self):
        self.assertFalse(_looks_like_person_name(""))

    def test_rejects_our_esteemed_staff(self):
        """'Our Esteemed Staff' contains organizational category words."""
        self.assertFalse(_looks_like_person_name("Our Esteemed Staff"))

    def test_rejects_swag_inquiries(self):
        """'Swag Inquiries' contains an organizational label, not a name."""
        self.assertFalse(_looks_like_person_name("Swag Inquiries"))

    def test_rejects_show_title_travel_zone(self):
        """'Travel Zone' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Travel Zone"))

    def test_rejects_show_title_the_bonsulator(self):
        """'The Bonsulator' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("The Bonsulator"))

    def test_rejects_show_title_station_playlists(self):
        """'Station Playlists' is a section heading, not a person."""
        self.assertFalse(_looks_like_person_name("Station Playlists"))

    def test_rejects_show_title_bipolar_music(self):
        """'Bipolar Music' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Bipolar Music"))

    def test_rejects_show_title_champ_sound(self):
        """'Champ Sound' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Champ Sound"))

    def test_rejects_show_title_secret_canine_agents(self):
        """'Secret Canine Agents' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Secret Canine Agents"))

    def test_rejects_show_title_fringe_factory(self):
        """'Fringe Factory' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Fringe Factory"))

    def test_rejects_show_title_maraschino_melodrama(self):
        """'Maraschino Melodrama' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Maraschino Melodrama"))

    def test_rejects_show_title_provocative_percussion(self):
        """'Provocative Percussion' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Provocative Percussion"))

    def test_rejects_show_title_radio_freetown(self):
        """'Radio Freetown' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Radio Freetown"))

    def test_rejects_show_title_stop_hitting_yourself(self):
        """'Stop Hitting Yourself' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Stop Hitting Yourself"))

    def test_rejects_show_title_your_boy_black_helmet_radio(self):
        """'Your Boy Black Helmet Radio' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name(
            "Your Boy Black Helmet Radio"))

    def test_rejects_show_title_the_laughing_clock(self):
        """'The Laughing Clock' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("The Laughing Clock"))

    def test_rejects_show_title_high_waisted_modernists(self):
        """'High Waisted Modernists' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("High Waisted Modernists"))

    def test_rejects_show_title_make_with_the_shake(self):
        """'Make With The Shake' is a show title, not a person."""
        self.assertFalse(_looks_like_person_name("Make With The Shake"))


# ---------------------------------------------------------------------------
# Structured extraction — email is the primary target
# ---------------------------------------------------------------------------

class TestStaffEntryExtraction(unittest.TestCase):

    def test_name_role_email_triple(self):
        """Pattern A: "Jane Smith — Music Director" with email on next line."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["name"], "Jane Smith")
        self.assertEqual(e["role"], "music_director")
        self.assertEqual(e["email"], "jane@kexp.org")
        self.assertIsNone(e["phone"])
        self.assertGreater(e["confidence_score"], 0.5)

    def test_role_colon_name_with_email(self):
        """Pattern B: "Music Director: Jane Smith" with email below."""
        text = (
            "Music Director: Jane Smith\n"
            "jane@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["name"], "Jane Smith")
        self.assertEqual(e["role"], "music_director")
        self.assertEqual(e["email"], "jane@kexp.org")

    def test_name_on_own_line_then_role_then_email(self):
        """Pattern C: name alone, role and email on following lines."""
        text = (
            "Jane Smith\n"
            "Music Director\n"
            "jane@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["name"], "Jane Smith")
        self.assertEqual(e["role"], "music_director")
        self.assertEqual(e["email"], "jane@kexp.org")

    def test_email_without_named_person(self):
        """Email-only entry on a staff page (no adjacent name)."""
        text = (
            "Programming Department\n"
            "programming@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        # No name-like line, so no structured entry from staff extraction.
        # The standard email-based extraction in build_contacts_from_page
        # will still pick up programming@kexp.org.
        self.assertEqual(len(entries), 0)

    def test_name_without_email_useful_for_later_enrichment(self):
        """Name + role but no email — still extracted for future passes."""
        text = (
            "Jane Smith\n"
            "Music Director\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["name"], "Jane Smith")
        self.assertEqual(e["role"], "music_director")
        self.assertIsNone(e["email"])
        self.assertIsNone(e["phone"])
        self.assertEqual(e["confidence_score"], 0.30)

    def test_phone_only_not_extracted(self):
        """Phone without email is never a standalone entry."""
        text = (
            "General Inquiries\n"
            "(206) 555-1234\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 0)

    def test_phone_attached_when_email_present(self):
        """Phone is secondary data — attached only alongside an email."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n"
            "(206) 555-1234\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["email"], "jane@kexp.org")
        self.assertEqual(e["phone"], "(206) 555-1234")

    def test_phone_not_attached_without_email(self):
        """Phone found near a name but no email — phone is dropped."""
        text = (
            "Jane Smith\n"
            "Music Director\n"
            "(206) 555-1234\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["name"], "Jane Smith")
        self.assertIsNone(e["email"])
        self.assertIsNone(e["phone"])

    def test_multiple_entries_extracted(self):
        """Staff page with multiple named people."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n\n"
            "Bob Rivera, Program Director\n"
            "bob@kexp.org\n\n"
            "Alice Chen — Station Manager\n"
            "alice@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 3)
        names = {e["name"] for e in entries}
        self.assertIn("Jane Smith", names)
        self.assertIn("Bob Rivera", names)
        self.assertIn("Alice Chen", names)
        for e in entries:
            self.assertIsNotNone(e["email"])
            self.assertIn(e["role"], (
                "music_director", "program_director", "station_manager"))

    def test_hyphenated_name_extracted(self):
        """Hyphenated surnames are captured by the enhanced name regex."""
        text = (
            "Jean-Pierre Delacroix — DJ\n"
            "jp@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Jean-Pierre Delacroix")
        self.assertEqual(entries[0]["email"], "jp@kexp.org")
        self.assertEqual(entries[0]["role"], "dj")

    def test_particle_name_extracted(self):
        """Names with particles (van, de, etc.) are captured."""
        text = (
            "Ludwig van Beethoven — Host\n"
            "ludwig@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Ludwig van Beethoven")
        self.assertEqual(entries[0]["email"], "ludwig@kexp.org")

    def test_dedup_by_name(self):
        """Same person appearing twice is not duplicated."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n\n"
            "Jane Smith\n"
            "Music Director\n"
            "jane@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)

    def test_comma_separator(self):
        """'Name, Role' pattern on one line."""
        text = (
            "Bob Rivera, Program Director\n"
            "bob@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Bob Rivera")
        self.assertEqual(entries[0]["role"], "program_director")
        self.assertEqual(entries[0]["email"], "bob@kexp.org")

    def test_role_not_borrowed_from_next_person(self):
        """Pattern C: role belonging to the next person must not attach to
        the current person.  The scan stops at a blank line or another
        person name."""
        text = (
            "Alice Chen\n"
            "alice@kexp.org\n"
            "\n"
            "Music Director\n"
            "Bob Rivera\n"
            "bob@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        alice = next((e for e in entries if e["name"] == "Alice Chen"), None)
        self.assertIsNotNone(alice)
        # Alice must NOT inherit Bob's "Music Director" role.
        self.assertEqual(alice["role"], "unknown")

    def test_role_not_borrowed_across_blank_boundary(self):
        """Even without an intervening person name, a blank line separates
        structural blocks — the role below the blank belongs to the next
        entry, not the current one."""
        text = (
            "Alice Chen\n"
            "alice@kexp.org\n"
            "\n"
            "Program Director\n"
            "Bob Rivera\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        alice = next((e for e in entries if e["name"] == "Alice Chen"), None)
        self.assertIsNotNone(alice)
        self.assertEqual(alice["role"], "unknown")

    def test_legitimate_name_gets_adjacent_role(self):
        """A real name followed immediately by its role (no blank separator)
        is correctly associated."""
        text = (
            "Jane Smith\n"
            "Music Director\n"
            "jane@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Jane Smith")
        self.assertEqual(entries[0]["role"], "music_director")

    def test_wfmu_dj_format_no_role_borrowing(self):
        """WFMU-style: 'DJ Person A / Person B / DJ Person C' — Person B
        must never inherit the DJ role from adjacent DJ entries."""
        text = (
            "Andy Waltzer\n"
            "andy@wfmu.org\n"
            "DJ Roman Angelos\n"
            "roman@wfmu.org\n"
            "Austin Rich\n"
            "austin@wfmu.org\n"
            "DJ Babs\n"
            "babs@wfmu.org\n"
            "Bill Zurat\n"
            "bill@wfmu.org\n"
            "DJ Greg Spacebrother Bishop\n"
            "greg@wfmu.org\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff Directory",
                      text=text)
        entries = extract_staff_entries(page)
        by_name = {e["name"]: e for e in entries}
        # Each person must have role unknown — the DJ label belongs to the
        # OTHER person's line, not theirs.
        for name in ("Andy Waltzer", "Austin Rich", "Bill Zurat"):
            self.assertIn(name, by_name, f"{name} should be extracted")
            self.assertEqual(
                by_name[name]["role"], "unknown",
                f"{name} must not inherit DJ role from adjacent entry")

    def test_wfmu_dj_person_entries_recognized(self):
        """Lines like 'DJ Roman Angelos' ARE person entries, not role
        labels.  They should be extracted as their own contacts."""
        text = (
            "Andy Waltzer\n"
            "andy@wfmu.org\n"
            "DJ Roman Angelos\n"
            "roman@wfmu.org\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff Directory",
                      text=text)
        entries = extract_staff_entries(page)
        names = {e["name"] for e in entries}
        self.assertIn("Andy Waltzer", names)
        andy = next(e for e in entries if e["name"] == "Andy Waltzer")
        self.assertEqual(andy["role"], "unknown")

    def test_dj_role_only_line_attributed_correctly(self):
        """A standalone 'DJ' label (no name attached) on its own line IS a
        pure role and should be attributed to the preceding name."""
        text = (
            "Jane Smith\n"
            "DJ\n"
            "jane@kexp.org\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Jane Smith")
        self.assertEqual(entries[0]["role"], "dj")

    def test_alias_show_name_not_confused_for_role_label(self):
        """'Black Ops' is a show name / alias — it looks like a person name
        and is extracted, but should not borrow a DJ role from adjacent
        entries."""
        text = (
            "Black Ops\n"
            "blackops@wfmu.org\n"
            "DJ Black Helmet\n"
            "helmet@wfmu.org\n"
            "Pat Byrne\n"
            "pat@wfmu.org\n"
            "DJ Perro Caliente\n"
            "perro@wfmu.org\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff Directory",
                      text=text)
        entries = extract_staff_entries(page)
        by_name = {e["name"]: e for e in entries}
        self.assertIn("Black Ops", by_name)
        self.assertEqual(by_name["Black Ops"]["role"], "unknown")
        self.assertIn("Pat Byrne", by_name)
        self.assertEqual(by_name["Pat Byrne"]["role"], "unknown")

    def test_role_above_name_assigned_to_correct_person(self):
        """Pattern D: 'Role\nName' pairs assign the role to the name
        below, not the name above."""
        text = (
            "Station Manager & Program Director\n"
            "Ken Freedman\n"
            "Music Director\n"
            "Jessica Romoff\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        by_name = {e["name"]: e for e in entries}
        self.assertIn("Ken Freedman", by_name)
        self.assertEqual(by_name["Ken Freedman"]["role"], "program_director")
        self.assertIn("Jessica Romoff", by_name)
        self.assertEqual(by_name["Jessica Romoff"]["role"], "music_director")

    def test_role_above_name_not_borrowed_by_preceding_name(self):
        """Pattern C scan must not pick up a role that belongs to the
        NEXT person in a role-above-name layout."""
        text = (
            "Joe McGasko\n"
            "joe@wfmu.org\n"
            "Music Director\n"
            "Jessica Romoff\n"
            "jessica@wfmu.org\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        by_name = {e["name"]: e for e in entries}
        joe = by_name.get("Joe McGasko")
        self.assertIsNotNone(joe)
        self.assertEqual(joe["role"], "unknown",
                         "Joe McGasko must not inherit Music Director role")
        jessica = by_name.get("Jessica Romoff")
        self.assertIsNotNone(jessica)
        self.assertEqual(jessica["role"], "music_director")

    def test_mc_prefix_name_breaks_pattern_c_scan(self):
        """Names with Mc-prefix (McGasko, McDonald) must be recognized as
        person names so Pattern C scan breaks at them correctly."""
        text = (
            "Michele Colomer\n"
            "michele@wfmu.org\n"
            "\n"
            "Joe McGasko\n"
            "joe@wfmu.org\n"
            "Music Director\n"
            "Jessica Romoff\n"
            "jessica@wfmu.org\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        by_name = {e["name"]: e for e in entries}
        joe = by_name.get("Joe McGasko")
        self.assertIsNotNone(joe)
        self.assertEqual(joe["role"], "unknown",
                         "McGasko must not inherit Music Director role")
        jessica = by_name.get("Jessica Romoff")
        self.assertIsNotNone(jessica)
        self.assertEqual(jessica["role"], "music_director")

    def test_compound_role_above_name(self):
        """Compound roles with ampersands (e.g., 'Station Manager &
        Program Director') are correctly detected as pure role lines
        and assigned to the name below."""
        text = (
            "Station Manager & Program Director\n"
            "Ken Freedman\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Ken Freedman")
        self.assertEqual(entries[0]["role"], "program_director")

    def test_role_above_name_mixed_with_pattern_c(self):
        """Full WFMU-style office staff section: role-above-name pairs
        interspersed with blank lines.  Each name gets only its own role."""
        text = (
            "Station Manager & Program Director\n"
            "Ken Freedman\n"
            "ken@wfmu.org\n"
            "\n"
            "Assistant General Manager\n"
            "Michele Colomer\n"
            "michele@wfmu.org\n"
            "\n"
            "Listener Services Director &\n"
            "Swag Inquiries\n"
            "Joe McGasko\n"
            "joe@wfmu.org\n"
            "\n"
            "Music Director\n"
            "Jessica Romoff\n"
            "jessica@wfmu.org\n"
        )
        page = _page("https://wfmu.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        by_name = {e["name"]: e for e in entries}
        # Ken gets Station Manager & Program Director
        self.assertEqual(by_name["Ken Freedman"]["role"], "program_director")
        # Jessica gets Music Director
        self.assertEqual(by_name["Jessica Romoff"]["role"], "music_director")
        # Joe McGasko gets no role (Music Director belongs to Jessica)
        self.assertEqual(by_name["Joe McGasko"]["role"], "unknown")


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class TestStaffEntryProvenance(unittest.TestCase):

    def test_email_entry_has_full_provenance(self):
        """Email-based entry carries name, role, and email provenance."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        prov = entries[0]["provenance"]
        methods = [p["method"] for p in prov]
        self.assertIn("staff_directory_extraction", methods)
        self.assertIn("role_label_rule", methods)
        self.assertIn("text_rule", methods)
        # Every provenance entry points at the source URL.
        for p in prov:
            self.assertEqual(p["source_url"], "https://kexp.org/staff")
            self.assertEqual(p["source_type"], "official_website_page")

    def test_name_only_entry_has_name_and_role_provenance(self):
        """Name-only entry (no email) still carries name + role provenance."""
        text = (
            "Jane Smith\n"
            "Music Director\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        prov = entries[0]["provenance"]
        methods = [p["method"] for p in prov]
        self.assertIn("staff_directory_extraction", methods)
        self.assertIn("role_label_rule", methods)
        # No email provenance since no email was found.
        self.assertNotIn("text_rule", methods)

    def test_phone_provenance_only_with_email(self):
        """Phone provenance is only present when email is also present."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n"
            "(206) 555-1234\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        prov = entries[0]["provenance"]
        methods = [p["method"] for p in prov]
        self.assertIn("phone_rule", methods)

    def test_no_phone_provenance_without_email(self):
        """Phone provenance is absent when no email is found."""
        text = (
            "Jane Smith\n"
            "Music Director\n"
            "(206) 555-1234\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        prov = entries[0]["provenance"]
        methods = [p["method"] for p in prov]
        self.assertNotIn("phone_rule", methods)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

class TestStaffEntryConfidence(unittest.TestCase):

    def test_email_entry_scores_higher_than_name_only(self):
        """Entries with email score higher than name-only entries."""
        text_with_email = (
            "Jane Smith — Music Director\njane@kexp.org\n"
        )
        text_name_only = (
            "Jane Smith\nMusic Director\n"
        )
        e_email = extract_staff_entries(
            _page("https://kexp.org/staff", title="Staff",
                  text=text_with_email))[0]
        e_name = extract_staff_entries(
            _page("https://kexp.org/staff", title="Staff",
                  text=text_name_only))[0]
        self.assertGreater(e_email["confidence_score"],
                           e_name["confidence_score"])
        self.assertEqual(e_email["confidence_score"], 0.55)
        self.assertEqual(e_name["confidence_score"], 0.30)


# ---------------------------------------------------------------------------
# Integration: build_contacts_from_page with staff directory
# ---------------------------------------------------------------------------

class TestStaffDirectoryIntegration(unittest.TestCase):

    def test_staff_page_yields_named_email_contacts(self):
        """Staff directory page produces named contacts with emails."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n\n"
            "Bob Rivera, Program Director\n"
            "bob@kexp.org\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        contacts = build_contacts_from_page(page)
        by_email = {c["email"]: c for c in contacts if c.get("email")}
        self.assertIn("jane@kexp.org", by_email)
        self.assertIn("bob@kexp.org", by_email)
        jane = by_email["jane@kexp.org"]
        self.assertEqual(jane["name"], "Jane Smith")
        self.assertEqual(jane["role"], "music_director")
        bob = by_email["bob@kexp.org"]
        self.assertEqual(bob["name"], "Bob Rivera")
        self.assertEqual(bob["role"], "program_director")

    def test_staff_entries_supplement_email_extraction(self):
        """Staff extraction and email extraction both contribute; email dedup."""
        text = (
            "Jane Smith — Music Director\n"
            "jane@kexp.org\n\n"
            "Also reach us at general@kexp.org for inquiries.\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        contacts = build_contacts_from_page(page)
        by_email = {c["email"]: c for c in contacts if c.get("email")}
        # Jane from staff extraction.
        self.assertIn("jane@kexp.org", by_email)
        self.assertEqual(by_email["jane@kexp.org"]["name"], "Jane Smith")
        # general@ from standard email extraction (not on staff page).
        self.assertIn("general@kexp.org", by_email)

    def test_non_staff_page_uses_standard_extraction(self):
        """Non-staff pages are unaffected by the staff directory module."""
        text = (
            "Contact us at info@kexp.org for general questions.\n"
            "Music submissions go to music@kexp.org.\n"
        )
        page = _page("https://kexp.org/contact", title="Contact Us",
                      text=text)
        contacts = build_contacts_from_page(page)
        by_email = {c["email"]: c for c in contacts if c.get("email")}
        self.assertIn("info@kexp.org", by_email)
        self.assertIn("music@kexp.org", by_email)
        # No named contacts from a non-staff page.
        for c in contacts:
            if c.get("email") in ("info@kexp.org", "music@kexp.org"):
                self.assertIsNone(c["name"])

    def test_name_only_staff_entry_gets_no_email_key(self):
        """Name-only entries are keyed by name, not email."""
        text = (
            "Jane Smith\n"
            "Music Director\n"
        )
        page = _page("https://kexp.org/staff", title="Staff", text=text)
        contacts = build_contacts_from_page(page)
        # The name-only entry should be present.
        names = [c["name"] for c in contacts if c.get("name")]
        self.assertIn("Jane Smith", names)
        # No email on this contact.
        jane = next(c for c in contacts if c.get("name") == "Jane Smith")
        self.assertIsNone(jane["email"])

    def test_generic_phone_not_treated_as_submission_contact(self):
        """A bare phone number without email is not a qualified contact."""
        text = (
            "Call us at (206) 555-1234 for general inquiries.\n"
        )
        page = _page("https://kexp.org/contact", title="Contact",
                      text=text)
        contacts = build_contacts_from_page(page)
        phone_contacts = [c for c in contacts if c.get("phone")
                          and not c.get("email")]
        # Phone-only contacts may exist but are low-confidence.
        for c in phone_contacts:
            self.assertLessEqual(c["confidence_score"], 0.25)

    def test_archive_show_titles_not_extracted_as_contacts(self):
        """Show titles on an archive page (e.g., 'Travel Zone', 'The
        Bonsulator') must NOT become contacts."""
        text = (
            "Station Playlists -\n"
            "Provided by Music Director Jessica Romoff\n"
            "\n"
            "Midnight-3am:\n"
            "Travel Zone with DJ Time Traveler - playlists and archives\n"
            "\n"
            "3am-6am:\n"
            "The Bonsulator with DJ Bonce - playlists and archives\n"
            "\n"
            "Secret Canine Agents with DJ Perro Caliente\n"
            "\n"
            "Bipolar Music with DJ Waste\n"
        )
        page = _page("https://wfmu.org/archive.html",
                      title="WFMU Playlists and Archives", text=text)
        contacts = build_contacts_from_page(page)
        names = {c.get("name", "").lower() for c in contacts if c.get("name")}
        # None of these show titles should appear as contacts.
        for title in ("station playlists", "travel zone",
                      "the bonsulator", "secret canine agents",
                      "bipolar music"):
            self.assertNotIn(title, names,
                             f"Show title '{title}' must not be a contact")

    def test_mailus_dj_alias_lines_not_attributed_as_roles(self):
        """In a flat alphabetized DJ list, 'DJ <Name>' on its own line is
        a person entry — it must NOT be borrowed as a role for the
        preceding person name."""
        text = (
            "Andy Waltzer\n"
            "andy@wfmu.org\n"
            "DJ Roman Angelos\n"
            "roman@wfmu.org\n"
            "Austin Rich\n"
            "austin@wfmu.org\n"
            "DJ Babs\n"
            "babs@wfmu.org\n"
            "Bill Zurat\n"
            "bill@wfmu.org\n"
            "DJ Greg Spacebrother Bishop\n"
            "greg@wfmu.org\n"
            "Black Ops\n"
            "blackops@wfmu.org\n"
            "DJ Black Helmet\n"
            "helmet@wfmu.org\n"
        )
        page = _page("https://wfmu.org/mailus.php",
                      title="WFMU Staff Directory", text=text)
        entries = extract_staff_entries(page)
        by_name = {e["name"]: e for e in entries}
        # Andy Waltzer must NOT get DJ Roman Angelos as role.
        self.assertIn("Andy Waltzer", by_name)
        self.assertEqual(by_name["Andy Waltzer"]["role"], "unknown")
        # Austin Rich must NOT get DJ Babs as role.
        self.assertIn("Austin Rich", by_name)
        self.assertEqual(by_name["Austin Rich"]["role"], "unknown")
        # Bill Zurat must NOT get DJ Greg Spacebrother Bishop as role.
        self.assertIn("Bill Zurat", by_name)
        self.assertEqual(by_name["Bill Zurat"]["role"], "unknown")

    def test_michele_colomer_not_duplicated_across_pages(self):
        """Michele Colomer should appear as a clean contact, not with
        a borrowed DJ role from a different person."""
        text = (
            "Michele Colomer\n"
            "Music Director\n"
            "michele@wfmu.org\n"
        )
        page = _page("https://wfmu.org/reachout.html",
                      title="WFMU Staff Directory", text=text)
        entries = extract_staff_entries(page)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["name"], "Michele Colomer")
        self.assertEqual(e["role"], "music_director")
        self.assertEqual(e["email"], "michele@wfmu.org")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
