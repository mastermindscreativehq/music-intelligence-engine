"""Regression tests for evidence-backed station-level Useful Pages.

Data-integrity guarantees under test:

1. An exact discovered href is preserved and surfaced unchanged.
2. A relative href is correctly resolved against its source page URL.
3. A URL can never be generated from a semantic label (no label -> URL).
4. Missing evidence yields NO fabricated Useful Page route.
5. Station-level Useful Pages stay separate from individual contacts/routes.
6. Existing Recommended Contacts / verified outreach behavior is untouched
   (guarded continuously by the other suites run in CI).

No network is used anywhere in this module.
"""

import unittest

from crawler.pages import parse_html

from discovery.radio.intelligence import (
    build_useful_pages,
    classify_useful_page,
)
from discovery.radio.schema import SourceFetchRecord

DOMAIN = "wfmu.org"
WFMU = f"https://{DOMAIN}"
HOME = f"{WFMU}/"
NESTED = f"{WFMU}/music/index.html"


def page(url: str, html: str):
    return parse_html(url, html)


class TestExactHrefPreserved(unittest.TestCase):
    """An exact discovered href is preserved and surfaced unchanged."""

    def test_absolute_href_kept_verbatim(self):
        html = '<a href="https://wfmu.org/music/djstaff">' \
               "DJ and staff email list</a>"
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(len(pages), 1)
        p = pages[0]
        self.assertEqual(p.url, "https://wfmu.org/music/djstaff")
        self.assertEqual(p.label, "DJ and staff email list")
        self.assertEqual(p.source_url, HOME)

    def test_anchor_label_is_the_discovered_text_not_the_url(self):
        html = '<a href="https://wfmu.org/940/send">Send Us Your Music</a>'
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(pages[0].url, "https://wfmu.org/940/send")
        self.assertEqual(pages[0].label, "Send Us Your Music")

    def test_category_is_preserved_with_the_url(self):
        html = '<a href="https://wfmu.org/chat">Talk to a DJ</a>'
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(pages[0].category, "dj_directory")
        self.assertEqual(pages[0].url, "https://wfmu.org/chat")


class TestRelativeResolved(unittest.TestCase):
    """A relative href is resolved against its source page URL."""

    def test_root_relative(self):
        html = '<a href="/send-music">Send Us Your Music</a>'
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(pages[0].url, "https://wfmu.org/send-music")
        self.assertEqual(pages[0].source_url, HOME)

    def test_scheme_relative_nested(self):
        # A page nested deeper: relative "djs" must resolve against
        # https://wfmu.org/music/ not /.
        html = '<a href="djs">DJ Directory</a>'
        pages = build_useful_pages([page(NESTED, html)], [], {DOMAIN})
        self.assertEqual(pages[0].url, "https://wfmu.org/music/djs")
        self.assertEqual(pages[0].source_url, NESTED)

    def test_absolute_href_untouched_by_resolution(self):
        html = '<a href="https://wfmu.org/about">About</a>'
        pages = build_useful_pages([page(NESTED, html)], [], {DOMAIN})
        self.assertEqual(pages[0].url, "https://wfmu.org/about")


class TestNoUrlFromLabel(unittest.TestCase):
    """A URL can never be constructed from a semantic label alone."""

    def test_label_does_not_stamp_a_route(self):
        # The anchor text screams "Send Music" but the real href is /940/.
        html = '<a href="/940">Send Us Your Music</a>'
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(pages[0].url, "https://wfmu.org/940")
        self.assertNotIn("send", pages[0].url.lower())

    def test_no_guessed_conventional_routes_are_invented(self):
        # Homepage has no useful links -> ZERO pages, not fabricated
        # /contact or /submissions routes.
        html = "<html><body><p>Welcome</p></body></html>"
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(pages, [])

    def test_classifier_is_pure_label_mapping(self):
        self.assertEqual(classify_useful_page("DJ home pages"),
                         "dj_directory")
        self.assertEqual(classify_useful_page("random prose"), "other")
        self.assertIsInstance(
            classify_useful_page("Send Us Your Music"), str)


class TestMissingEvidence(unittest.TestCase):
    """No evidence -> no fabricated Useful Page route."""

    def test_empty_pages_produce_nothing(self):
        self.assertEqual(build_useful_pages([], [], {DOMAIN}), [])

    def test_mailto_javascript_tel_empty_rejected(self):
        html = ('<a href="mailto:dj@wfmu.org">Email</a>'
                '<a href="javascript:void(0)">JS</a>'
                '<a href="tel:+15551234567">Call</a>'
                '<a href="">Empty</a>')
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(pages, [])

    def test_offsite_links_not_surfaced_as_station_pages(self):
        html = ('<a href="https://external.example/contact">Contact</a>'
                '<a href="https://wfmu.org/about">About</a>')
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        urls = [p.url for p in pages]
        self.assertIn("https://wfmu.org/about", urls)
        self.assertNotIn("https://external.example/contact", urls)

    def test_self_link_not_a_route(self):
        html = '<a href="https://wfmu.org/">Homepage</a>'
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(pages, [])

    def test_unreachable_discovered_url_kept_not_replaced(self):
        # A discovered link whose exact URL was fetched-and-failed is kept,
        # marked unreachable, never replaced with a guessed URL.
        html = '<a href="https://wfmu.org/broken-contact">Contact Us</a>'
        rec = SourceFetchRecord(url="https://wfmu.org/broken-contact",
                                ok=False, status=404)
        pages = build_useful_pages([page(HOME, html)], [rec], {DOMAIN})
        self.assertEqual(pages[0].url, "https://wfmu.org/broken-contact")
        self.assertFalse(pages[0].reachable)
        self.assertEqual(pages[0].status, 404)


class TestStationContactSeparation(unittest.TestCase):
    """Station-level Useful Pages remain separate from individual contacts."""

    def test_useful_pages_have_no_person_fields(self):
        html = ('<a href="https://wfmu.org/djs">DJ home pages</a>'
                '<a href="https://wfmu.org/send">Send Us Your Music</a>')
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertTrue(pages)
        for p in pages:
            self.assertIsNone(getattr(p, "email", None))
            self.assertEqual(getattr(p, "role", "unknown"), "unknown")
            self.assertIsNone(getattr(p, "phone", None))

    def test_dedup_keeps_one_entry_per_exact_url(self):
        html = ('<a href="https://wfmu.org/about">About</a>'
                '<a href="https://wfmu.org/about">About Us (repeated)</a>')
        pages = build_useful_pages([page(HOME, html)], [], {DOMAIN})
        self.assertEqual(len(pages), 1)


if __name__ == "__main__":
    unittest.main()
