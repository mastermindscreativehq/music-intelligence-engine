"""Phase 2 tests: URL normalization and canonical domains."""

import unittest

from crawler.urls import (
    InvalidUrlError,
    canonical_domain,
    canonical_host,
    normalize_url,
    same_site,
    slugify_name,
)


class TestUrlNormalization(unittest.TestCase):
    def test_preserves_meaningful_query_params(self):
        self.assertEqual(
            normalize_url("https://station.example/page?id=7&x=2"),
            "https://station.example/page?id=7&x=2",
        )

    def test_sorts_remaining_query_params(self):
        self.assertEqual(
            normalize_url("https://station.example/page?z=1&a=2"),
            "https://station.example/page?a=2&z=1",
        )

    def test_strips_tracking_parameters(self):
        self.assertEqual(
            normalize_url(
                "https://station.example/page?utm_source=x&id=7"),
            "https://station.example/page?id=7",
        )

    def test_strips_fragment_and_www_and_trailing_slash(self):
        self.assertEqual(
            normalize_url("http://WWW.Station.Example/about/#team"),
            "http://station.example/about",
        )

    def test_scheme_is_preserved_not_upgraded(self):
        # Never invent security properties: an http candidate stays http.
        self.assertEqual(normalize_url("http://station.example/"),
                         "http://station.example/")

    def test_bare_domain_defaults_to_https(self):
        self.assertEqual(normalize_url("Station.example"),
                         "https://station.example")

    def test_rejects_non_http_schemes(self):
        for bad in ("ftp://station.example", "file:///etc/passwd",
                    "javascript:alert(1)"):
            with self.subTest(url=bad):
                with self.assertRaises(InvalidUrlError):
                    normalize_url(bad)

    def test_rejects_empty_and_hostless(self):
        for bad in ("", "   ", "not-a-url"):
            with self.subTest(url=bad):
                with self.assertRaises(InvalidUrlError):
                    normalize_url(bad)

    def test_keeps_port(self):
        self.assertEqual(
            normalize_url("https://station.example:8080/x"),
            "https://station.example:8080/x")


class TestCanonicalIdentity(unittest.TestCase):
    def test_canonical_host(self):
        self.assertEqual(canonical_host("WWW.Station.example:8080/path"),
                         "station.example")

    def test_canonical_domain_simple(self):
        self.assertEqual(canonical_domain("https://www.kqxr.example/a"),
                         "kqxr.example")

    def test_canonical_domain_multi_part_suffix(self):
        self.assertEqual(canonical_domain("shop.bbc.co.uk"), "bbc.co.uk")

    def test_canonical_domain_single_label(self):
        self.assertEqual(canonical_domain("localhost"), "localhost")

    def test_same_site(self):
        self.assertTrue(same_site("https://a.station.example/x",
                                  "http://station.example/y"))
        self.assertFalse(same_site("https://one.example/",
                                   "https://two.example/"))

    def test_slugify_name(self):
        self.assertEqual(slugify_name("Radio 88.3 FM!"), "radio-88-3-fm")


if __name__ == "__main__":
    unittest.main()
