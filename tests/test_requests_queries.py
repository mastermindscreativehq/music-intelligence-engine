"""Phase 2 tests: discovery requests and query generation."""

import unittest

from discovery.models import Candidate, DiscoveryRequest, SourceType
from discovery.queries import build_queries


def make_request(**overrides):
    base = {"query": "independent radio stations"}
    base.update(overrides)
    return DiscoveryRequest.from_dict(base)


class TestDiscoveryRequest(unittest.TestCase):
    def test_from_dict_accepts_valid(self):
        request = make_request(country="United States",
                               state_or_region="New York", limit=10)
        self.assertEqual(request.limit, 10)
        self.assertEqual(request.state_or_region, "New York")

    def test_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            DiscoveryRequest.from_dict({"query": "x", "hacker": True})

    def test_rejects_empty_query(self):
        for bad in ("", "   ", None, 123):
            with self.subTest(query=bad):
                with self.assertRaises(ValueError):
                    DiscoveryRequest.from_dict({"query": bad})

    def test_rejects_bad_limits(self):
        for bad in (0, -1, 501, "ten", 2.5, True):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    make_request(limit=bad)

    def test_limit_bounds_ok(self):
        self.assertEqual(make_request(limit=1).limit, 1)
        self.assertEqual(make_request(limit=500).limit, 500)

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            DiscoveryRequest.from_dict(["not", "a", "dict"])


class TestCandidate(unittest.TestCase):
    def test_rejects_empty_url(self):
        with self.assertRaises(ValueError):
            Candidate(title="t", url="  ", source="s")

    def test_source_type_fallback(self):
        candidate = Candidate(title="t", url="https://x.example/",
                              source="s", source_type="weird")
        self.assertEqual(candidate.source_type, SourceType.OTHER)


class TestQueryGeneration(unittest.TestCase):
    def test_full_request_variants(self):
        queries = build_queries(make_request(
            station_type="college", genre="jazz",
            city="Austin", state_or_region="Texas",
            country="United States"))
        self.assertEqual(queries[0],
                         "college jazz radio stations Austin Texas United States")
        self.assertIn("college jazz radio stations accepting music "
                      "submissions Austin Texas United States", queries)
        self.assertIn("radio stations Austin Texas United States contact",
                      queries)

    def test_minimal_request(self):
        queries = build_queries(DiscoveryRequest(query="radio"))
        self.assertEqual(queries, ["radio stations"])

    def test_no_geography_skips_contact_variant(self):
        queries = build_queries(make_request(station_type="community"))
        self.assertNotIn("contact", " ".join(queries))

    def test_deterministic(self):
        request = make_request(station_type="independent",
                               state_or_region="California")
        self.assertEqual(build_queries(request), build_queries(request))

    def test_cap_on_queries(self):
        from discovery.queries import MAX_QUERIES
        queries = build_queries(make_request(
            station_type="community", genre="afrobeat",
            city="Houston", state_or_region="Texas",
            country="United States"))
        self.assertLessEqual(len(queries), MAX_QUERIES)


if __name__ == "__main__":
    unittest.main()
