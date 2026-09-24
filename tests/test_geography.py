"""Evidence-based geography extraction (enrichment.geography).

The extractor resolves a station's location strictly from its own fetched
pages, homepage title, and registrable domain — never from a discovery
request's scope or a search snippet.
"""

from __future__ import annotations

import unittest

from enrichment.geography import extract_location


class Page:
    def __init__(self, url: str, text: str, title: str = ""):
        self.url = url
        self.text = text
        self.title = title


def extract(*, url, title="", body="", pages=None):
    if pages is None:
        pages = [Page(url, body)] if body else []
    return extract_location(url=url, title=title, pages=pages)


class TestTitleAndRegionEvidence(unittest.TestCase):
    def test_chly_title_resolves_nanaimo_bc_canada(self):
        loc = extract(url="https://chly.ca/submissions",
                      title="Submit your Music - CHLY 101.7FM: "
                            "Listener Supported in Nanaimo BC.")
        self.assertEqual(loc.country, "Canada")
        self.assertEqual(loc.state_or_region, "British Columbia")
        self.assertEqual(loc.city, "Nanaimo")
        methods = {e["method"] for e in loc.evidence}
        self.assertIn("title_rule", methods)

    def test_page_city_region_token(self):
        loc = extract(url="https://kxt.org/",
                      title="KXT 91.7",
                      body="Mailing: Suite 300, Dallas, TX 75315.")
        self.assertEqual(loc.country, "United States")
        self.assertEqual(loc.state_or_region, "Texas")
        self.assertEqual(loc.city, "Dallas")

    def test_multi_word_city_captured_whole(self):
        loc = extract(url="https://wyso.org/",
                      title="WYSO Public Radio",
                      body="Yellow Springs, OH 45387")
        self.assertEqual(loc.city, "Yellow Springs")
        self.assertEqual(loc.state_or_region, "Ohio")

    def test_country_token_in_comma_context(self):
        loc = extract(url="https://station.tokyo/",
                      title="Tokyo FM", body="We broadcast in Tokyo, Japan.")
        self.assertEqual(loc.country, "Japan")

    def test_street_address_prefix_does_not_supplant_city(self):
        loc = extract(url="https://thecurrent.example/",
                      title="The Current",
                      body="Cedar St. Suite 200, St. Paul, MN 55101")
        self.assertEqual(loc.city, "St. Paul")
        self.assertEqual(loc.state_or_region, "Minnesota")

    def test_saint_city_starts_with_st(self):
        loc = extract(url="https://other.example/",
                      title="Station",
                      body="P.O. Box 500, St. Paul, MN")
        self.assertEqual(loc.city, "St. Paul")

    def test_full_region_in_prose(self):
        loc = extract(url="https://reader.example/",
                      title="Cedar Valley Reader",
                      body="Serving the people of British Columbia.")
        self.assertEqual(loc.country, "Canada")
        self.assertEqual(loc.state_or_region, "British Columbia")


class TestNegativeEvidence(unittest.TestCase):
    def test_send_us_your_music_is_not_a_country(self):
        loc = extract(url="https://shadypinesradio.com/",
                      title="send us a song | Shady Pines Radio",
                      body="Shady Pines Radio is community radio. Send us "
                           "your music any time.")
        self.assertIsNone(loc.country)
        self.assertIsNone(loc.state_or_region)
        self.assertIsNone(loc.city)

    def test_broadcasting_from_without_region_is_not_a_city(self):
        # "broadcasting from Cedar Valley" names no state/province; the city
        # caption rule requires an explicit region token.
        loc = extract(url="https://kqxr.example/",
                      title="KQXR 101.5 FM | Community Radio for Cedar Valley",
                      body="Community radio, listener-supported, broadcasting "
                           "from Cedar Valley.")
        self.assertEqual(loc.country, "United States")  # callsign only
        self.assertIsNone(loc.state_or_region)
        self.assertIsNone(loc.city)

    def test_listener_qa_page_never_invents_location(self):
        loc = extract(url="https://thecurrent.org/qa",
                      title="The Current", body="Ask us anything.")
        self.assertIsNone(loc.country)


class TestDomainAndCallsignCorroboration(unittest.TestCase):
    def test_ca_tld_sets_country_when_text_silent(self):
        loc = extract(url="https://bandcamp.example.ca/", title="Some Station")
        self.assertEqual(loc.country, "Canada")
        self.assertEqual(loc.evidence[0]["method"], "domain_tld_rule")

    def test_text_evidence_wins_over_tld(self):
        loc = extract(url="https://chly.example.ca/",
                      title="CHLY 101.7FM",
                      body="Broadcasting from Nanaimo, British Columbia.")
        self.assertEqual(loc.country, "Canada")
        self.assertEqual(loc.state_or_region, "British Columbia")

    def test_us_callsign_resolves_country(self):
        loc = extract(url="https://wyso.example/",
                      title="WYSO 91.3 | Public Radio", body="")
        self.assertEqual(loc.country, "United States")

    def test_canadian_callsign_resolves_country(self):
        loc = extract(url="https://chly.example/",
                      title="CHLY 101.7FM", body="")
        self.assertEqual(loc.country, "Canada")

    def test_no_frequency_means_no_callsign_inference(self):
        loc = extract(url="https://kq-family.example/",
                      title="KQ Family Radio", body="")
        self.assertIsNone(loc.country)


class TestAbsence(unittest.TestCase):
    def test_empty_site_yields_nothing(self):
        loc = extract(url="https://nogeo.example/",
                      title="Untitled Station",
                      body="Independent radio on your dial.")
        self.assertIsNone(loc.country)
        self.assertIsNone(loc.state_or_region)
        self.assertIsNone(loc.city)
        self.assertEqual(loc.evidence, [])


if __name__ == "__main__":
    unittest.main()