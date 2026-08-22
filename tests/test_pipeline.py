"""Phase 2 end-to-end pipeline tests using deterministic fixtures.

No network access, no credentials: a FakeFetcher serves fixture HTML and
programmed failures; providers are static/seed-file backed.
"""

import json
import unittest
from pathlib import Path

from crawler.http import FetchResult
from crawler.urls import normalize_url

from discovery.models import DiscoveryRequest, SourceType
from discovery.providers import SeedListProvider, StaticListProvider
from discovery.radio.pipeline import EngineConfig, RadioDiscoveryEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGES = FIXTURES / "pages"


def load(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


class FakeFetcher:
    """URL-keyed fetcher double; misses produce dns_error."""

    def __init__(self, pages: dict[str, dict]):
        self.pages = {
            normalize_url(url): entry for url, entry in pages.items()
        }
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        try:
            key = normalize_url(url)
        except ValueError:
            key = url
        entry = self.pages.get(key)
        result = FetchResult(url=url)
        if entry is None:
            result.error_kind = "dns_error"
            result.error_message = "fixture miss"
            return result
        if "error_kind" in entry:
            result.error_kind = entry["error_kind"]
            result.error_message = entry.get("error_message", "programmed")
            return result
        result.status = entry.get("status", 200)
        result.content_type = entry.get("content_type", "text/html")
        result.body = entry.get("body", "")
        result.final_url = url
        return result


KQXR_HOME = load("kqxr_home.html")
KQXR_CONTACT = load("kqxr_contact.html")
KQXR_SUBMISSIONS = load("kqxr_submissions.html")
WXYZ_HOME = load("wxyz_home.html")
WLDG_MULTI = load("multi_email.html")
MALFORMED = load("malformed.html")


def default_pages() -> dict:
    return {
        "https://kqxr.example/": {"body": KQXR_HOME},
        "https://kqxr.example/contact": {"body": KQXR_CONTACT},
        "https://kqxr.example/submissions": {"body": KQXR_SUBMISSIONS},
        "https://wxyz.example/": {"body": WXYZ_HOME},
        "https://wldg.example/home.html": {"body": WLDG_MULTI},
        "https://brokenstation.example/malformed": {"body": MALFORMED},
    }


def make_engine(pages=None):
    return RadioDiscoveryEngine(
        provider=None,
        fetcher=FakeFetcher(pages or default_pages()),
        config=EngineConfig(crawl_delay_seconds=0.0,
                            respect_robots=False,
                            max_pages_per_site=6),
    )


def run_with(provider_entries, pages=None, **request_kwargs):
    engine = make_engine(pages)
    engine.provider = StaticListProvider(provider_entries)
    request = DiscoveryRequest.from_dict(request_kwargs or
                                         {"query": "radio stations"})
    return engine.run(request)


class TestHappyPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_with([
            {"name": "KQXR 101.5 FM", "url": "https://kqxr.example/"},
        ])

    def test_one_normalized_record(self):
        self.assertEqual(self.result.record_count, 1)

    def test_emails_have_provenance(self):
        record = self.result.records[0]
        values = {e["value"]: e for e in record["emails"]}
        self.assertIn("music@kqxr.example", values)
        self.assertEqual(values["music@kqxr.example"]["source_url"],
                         "https://kqxr.example/submissions")
        self.assertEqual(values["info@kqxr.example"]["source_type"],
                         "official_website_page")

    def test_submission_and_contact_pages_found(self):
        record = self.result.records[0]
        self.assertIsNotNone(record["submission_url"])
        self.assertEqual(record["submission_url"]["value"],
                         "https://kqxr.example/submissions")
        self.assertIsNotNone(record["contact_url"])

    def test_named_music_director_contact(self):
        record = self.result.records[0]
        jane = next((c for c in record["contacts"]
                     if c["email"] == "music@kqxr.example"), None)
        self.assertIsNotNone(jane)
        self.assertEqual(jane["role"], "music_director")
        self.assertEqual(jane["name"], "Jane Smith")

    def test_classification_community(self):
        record = self.result.records[0]
        self.assertEqual(record["station_type"], "community")
        self.assertTrue(record["classification_evidence"])

    def test_confidence_reasonable_and_explained(self):
        record = self.result.records[0]
        self.assertGreater(record["confidence_score"], 0.6)
        self.assertTrue(record["confidence_reasons"])

    def test_socials_recorded(self):
        socials = self.result.records[0]["social_urls"]
        self.assertIn("facebook", socials)
        self.assertIn("instagram", socials)


class TestDuplicateCandidates(unittest.TestCase):
    def test_www_alias_candidates_merge_to_single_record(self):
        result = run_with([
            {"name": "KQXR", "url": "https://kqxr.example/"},
            {"name": "KQXR", "url": "http://www.kqxr.example/"},
            {"name": "KQXR", "url": "https://kqxr.example/"},
        ])
        self.assertEqual(result.record_count, 1)
        record = result.records[0]
        self.assertGreaterEqual(len(record["source_urls"]), 1)


class TestFailureHandling(unittest.TestCase):
    def test_timeout_creates_broken_record_and_continues(self):
        pages = default_pages()
        pages["https://brokenstation.example/"] = {
            "error_kind": "timeout"}
        result = run_with([
            {"name": "KQXR", "url": "https://kqxr.example/"},
            {"name": "Broken Signal", "url": "https://brokenstation.example/"},
        ], pages=pages)
        kinds = {f.error_kind for f in result.failures}
        self.assertIn("timeout", kinds)
        broken = next(r for r in result.records
                      if r["website"] == "https://brokenstation.example/")
        self.assertFalse(broken["website_reachable"])
        self.assertLess(broken["confidence_score"], 0.35)
        kqxr = next(r for r in result.records if r["name"].startswith("KQXR"))
        self.assertTrue(kqxr["website_reachable"])

    def test_robots_disallowed_recorded(self):
        pages = default_pages()
        pages["https://wxyz.example/"] = {
            "error_kind": "robots_disallowed"}
        result = run_with(
            [{"name": "WXYZ", "url": "https://wxyz.example/"}],
            pages=pages)
        kinds = {f.error_kind for f in result.failures}
        self.assertIn("robots_disallowed", kinds)

    def test_malformed_page_does_not_crash_run(self):
        result = run_with([
            {"name": "Broken Signal", "url":
             "https://brokenstation.example/malformed"},
        ])
        self.assertEqual(result.record_count, 1)
        record = result.records[0]
        self.assertIn("info@brokenstation.example",
                      [e["value"] for e in record["emails"]])

    def test_invalid_candidate_url_recorded(self):
        result = run_with([
            {"name": "?", "url": "not-a-url"},
            {"name": "KQXR", "url": "https://kqxr.example/"},
        ])
        stages = {(f.stage, f.error_kind) for f in result.failures}
        self.assertIn(("url_normalization", "invalid_url"), stages)
        self.assertEqual(result.record_count, 1)

    def test_multiple_emails_and_roles_on_one_page(self):
        result = run_with([
            {"name": "WLDG Freeform Radio",
             "url": "https://wldg.example/home.html"},
        ])
        record = result.records[0]
        values = {e["value"] for e in record["emails"]}
        self.assertIn("info@wldg.example", values)
        self.assertIn("music@wldg.example", values)
        self.assertIn("dana@wldg.example", values)
        # Obfuscated / malformed candidates must NOT appear.
        self.assertNotIn("contact@station.org", values)
        self.assertFalse(any(v.startswith("bad@@") for v in values))


class TestEmptyAndLimits(unittest.TestCase):
    def test_empty_provider_yields_clean_result(self):
        result = run_with([])
        self.assertEqual(result.record_count, 0)
        self.assertEqual(result.failure_count, 0)
        self.assertIsNotNone(result.to_dict()["completed_at"])

    def test_limit_caps_processed_domains(self):
        entries = [
            {"name": f"S{i}", "url": f"https://site{i}.example/"}
            for i in range(10)
        ]
        engine = make_engine()
        engine.provider = StaticListProvider(entries)
        result = engine.run(DiscoveryRequest.from_dict(
            {"query": "stations", "limit": 3}))
        self.assertLessEqual(result.record_count, 3)

    def test_seed_provider_respects_limit_and_shape(self):
        provider = SeedListProvider(FIXTURES / "seeds" / "test_seed.json")
        request = DiscoveryRequest.from_dict({"query": "x", "limit": 2})
        candidates = provider.search(request,
                                     build_queries_fallback(request))
        self.assertLessEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertEqual(candidate.source_type,
                             SourceType.DIRECTORY_SOURCE)

    def test_seed_provider_geography_filter(self):
        provider = SeedListProvider(FIXTURES / "seeds" / "test_seed.json")
        request = DiscoveryRequest.from_dict({
            "query": "x", "state_or_region": "Cedar State"})
        candidates = provider.search(request, [])
        urls = {c.url for c in candidates}
        self.assertEqual(urls, {"https://kqxr.example/",
                                "http://www.kqxr.example:80/"})


def build_queries_fallback(request):
    from discovery.queries import build_queries
    return build_queries(request)


class TestStructuredOutput(unittest.TestCase):
    def test_result_serializes_to_json(self):
        result = run_with([
            {"name": "KQXR", "url": "https://kqxr.example/"}])
        blob = json.dumps(result.to_dict())
        data = json.loads(blob)
        self.assertIn("records", data)
        self.assertIn("failures", data)
        self.assertIn("queries", data)


if __name__ == "__main__":
    unittest.main()
