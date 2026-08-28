"""Tests for automatic internal page discovery in the enrichment engine.

Phase 2: discovers staff/team/people/contact/submission pages from
fetched pages and fetches them with bounded budget.
"""

import unittest

from crawler.http import FetchResult
from crawler.pages import parse_html

from discovery.radio.enrich_pipeline import EnrichmentEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HOMEPAGE_HTML = """\
<html><body>
<h1>My Radio Station</h1>
<p>Welcome to KZZZ.</p>
<a href="/staff">Meet Our Staff</a>
<a href="/team">The Team</a>
<a href="/contact-us">Contact Us</a>
<a href="/submissions">Submit Music</a>
<a href="/about">About Us</a>
<a href="/shows">Our Shows</a>
<a href="https://external.example/partner">Partner</a>
</body></html>
"""

STAFF_HTML = """\
<html><body>
<h1>Our Staff</h1>
<p>Station Manager: <strong>Jane Doe</strong></p>
<p>Email: jane@kzzz.example</p>
<p>Music Director: <strong>Bob Smith</strong></p>
<p>Email: bob@kzzz.example</p>
</body></html>
"""

SUBMISSIONS_HTML = """\
<html><body>
<h1>Music Submissions</h1>
<p>Submit your music to programming@kzzz.example</p>
</body></html>
"""

CONTACT_HTML = """\
<html><body>
<h1>Contact Us</h1>
<p>General inquiries: info@kzzz.example</p>
</body></html>
"""

ABOUT_HTML = """\
<html><body>
<h1>About KZZZ</h1>
<p>We are a community radio station.</p>
</body></html>
"""

SHOWS_HTML = """\
<html><body>
<h1>Our Shows</h1>
<p>Tune in weekdays.</p>
</body></html>
"""

BASE = "https://kzzz.example"
HOME_URL = f"{BASE}/"
STAFF_URL = f"{BASE}/staff"
TEAM_URL = f"{BASE}/team"
CONTACT_URL = f"{BASE}/contact-us"
SUBMISSIONS_URL = f"{BASE}/submissions"
ABOUT_URL = f"{BASE}/about"
SHOWS_URL = f"{BASE}/shows"


class FakeFetcher:
    """Fetches from a dict of URL -> HTML, records all fetch calls."""

    def __init__(self, pages: dict[str, str]):
        self.pages = dict(pages)
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        result = FetchResult(url=url)
        body = self.pages.get(url)
        if body is None:
            result.error_kind = "dns_error"
            return result
        result.status = 200
        result.content_type = "text/html"
        result.body = body
        result.final_url = url
        return result


def make_record(**overrides) -> dict:
    record = {
        "id": "test-station",
        "name": "KZZZ 99.1 FM",
        "website": HOME_URL,
        "station_type": "community",
        "genres": [],
        "formats": [],
        "social_urls": {},
        "source_urls": [HOME_URL],
        "confidence_score": 0.5,
        "confidence_reasons": [],
        "status": "discovered",
        "emails": [],
        "contacts": [],
        "phone_numbers": [],
        "submission_url": None,
        "contact_url": None,
        "programming_url": None,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# _discover_internal_pages unit tests
# ---------------------------------------------------------------------------

class TestDiscoverInternalPages(unittest.TestCase):

    def _engine(self, **config_overrides):
        return EnrichmentEngine(config=type("C", (), {
            "max_pages_per_station": config_overrides.get("budget", 6),
            "timeout_seconds": 5.0,
            "rate_limit_seconds": 0.0,
            "respect_robots": False,
            "user_agent": "test",
            "logger": None,
        })())

    def test_empty_when_no_pages(self):
        engine = self._engine()
        result = engine._discover_internal_pages([], [], 5)
        self.assertEqual(result, [])

    def test_empty_when_zero_budget(self):
        engine = self._engine()
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        result = engine._discover_internal_pages([page], [HOME_URL], 0)
        self.assertEqual(result, [])

    def test_offsite_links_excluded(self):
        engine = self._engine()
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        result = engine._discover_internal_pages([page], [HOME_URL], 5)
        for url in result:
            self.assertIn("kzzz.example", url)

    def test_already_fetched_excluded(self):
        engine = self._engine()
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        result = engine._discover_internal_pages(
            [page], [HOME_URL, STAFF_URL, TEAM_URL, CONTACT_URL,
                     SUBMISSIONS_URL], 5)
        for url in result:
            self.assertNotIn(url, [HOME_URL, STAFF_URL, TEAM_URL,
                                    CONTACT_URL, SUBMISSIONS_URL])

    def test_respects_budget(self):
        engine = self._engine(budget=2)
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        result = engine._discover_internal_pages([page], [HOME_URL], 2)
        self.assertLessEqual(len(result), 2)

    def test_higher_weight_links_ranked_first(self):
        engine = self._engine(budget=10)
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        result = engine._discover_internal_pages([page], [HOME_URL], 10)
        # submissions (weight 10) and contact-us (weight 8) should beat
        # about (weight 2) and shows (weight 1)
        weights = []
        for url in result:
            if "submissions" in url:
                weights.append(10)
            elif "contact" in url:
                weights.append(8)
            elif "staff" in url or "team" in url:
                weights.append(5)
            elif "about" in url:
                weights.append(2)
            elif "shows" in url:
                weights.append(1)
            else:
                weights.append(0)
        # Weights should be non-increasing
        for i in range(len(weights) - 1):
            self.assertGreaterEqual(weights[i], weights[i + 1])


# ---------------------------------------------------------------------------
# Integration: engine discovers and fetches internal pages
# ---------------------------------------------------------------------------

class TestEnrichmentEnginePageDiscovery(unittest.TestCase):

    def _make_engine_and_fetcher(self, **config_overrides):
        budget = config_overrides.pop("budget", 6)
        pages = {
            HOME_URL: HOMEPAGE_HTML,
            STAFF_URL: STAFF_HTML,
            TEAM_URL: STAFF_HTML,  # same content for simplicity
            CONTACT_URL: CONTACT_HTML,
            SUBMISSIONS_URL: SUBMISSIONS_HTML,
            ABOUT_URL: ABOUT_HTML,
            SHOWS_URL: SHOWS_HTML,
        }
        fetcher = FakeFetcher(pages)
        engine = EnrichmentEngine(fetcher=fetcher)
        engine.config.max_pages_per_station = budget
        return engine, fetcher

    def test_discover_and_fetch_staff_pages(self):
        engine, fetcher = self._make_engine_and_fetcher(budget=6)
        record = make_record()
        result = engine.enrich_records([record])
        enriched = result.records[0]

        # Staff page should have been discovered and fetched
        fetched_urls = set(fetcher.fetched)
        self.assertIn(STAFF_URL, fetched_urls)
        # Contacts from staff page should be extracted
        contacts = enriched.get("contacts", [])
        emails = {c.get("email") for c in contacts}
        self.assertIn("jane@kzzz.example", emails)
        self.assertIn("bob@kzzz.example", emails)

    def test_budget_respected_total(self):
        engine, fetcher = self._make_engine_and_fetcher(budget=3)
        record = make_record()
        engine.enrich_records([record])
        # Should fetch at most 3 pages total
        self.assertLessEqual(len(fetcher.fetched), 3)

    def test_discovered_contacts_have_source_url(self):
        engine, fetcher = self._make_engine_and_fetcher(budget=6)
        record = make_record()
        result = engine.enrich_records([record])
        enriched = result.records[0]
        contacts = enriched.get("contacts", [])
        for contact in contacts:
            source = contact.get("source_url")
            self.assertIsNotNone(source)
            self.assertTrue(
                source.startswith("https://"),
                f"source_url should be absolute: {source}")

    def test_submission_email_found_on_discovered_page(self):
        engine, fetcher = self._make_engine_and_fetcher(budget=6)
        record = make_record()
        result = engine.enrich_records([record])
        enriched = result.records[0]
        emails = {e["value"] for e in enriched.get("emails", [])}
        self.assertIn("programming@kzzz.example", emails)

    def test_offline_mode_no_fetches(self):
        engine = EnrichmentEngine()
        record = make_record()
        result = engine.enrich_records([record])
        enriched = result.records[0]
        self.assertEqual(enriched["fetches"], [])
        self.assertEqual(enriched["raw_metadata"]["enrichment_mode"],
                         "offline_facts_only")


# ---------------------------------------------------------------------------
# Budget boundary tests
# ---------------------------------------------------------------------------

class TestBudgetBoundary(unittest.TestCase):

    def test_budget_one_fetches_only_homepage(self):
        """With budget=1, only the initial URL (homepage) is fetched."""
        pages = {
            HOME_URL: HOMEPAGE_HTML,
            STAFF_URL: STAFF_HTML,
        }
        fetcher = FakeFetcher(pages)
        engine = EnrichmentEngine(fetcher=fetcher)
        engine.config.max_pages_per_station = 1
        record = make_record()
        engine.enrich_records([record])
        self.assertEqual(len(fetcher.fetched), 1)
        self.assertEqual(fetcher.fetched[0], HOME_URL)

    def test_budget_two_fetches_homepage_plus_one_discovery(self):
        """With budget=2, homepage + 1 discovered page."""
        pages = {
            HOME_URL: HOMEPAGE_HTML,
            SUBMISSIONS_URL: SUBMISSIONS_HTML,
            STAFF_URL: STAFF_HTML,
        }
        fetcher = FakeFetcher(pages)
        engine = EnrichmentEngine(fetcher=fetcher)
        engine.config.max_pages_per_station = 2
        record = make_record()
        engine.enrich_records([record])
        self.assertEqual(len(fetcher.fetched), 2)
        self.assertIn(HOME_URL, fetcher.fetched)
        # The second fetch should be the highest-priority discovered page
        # (submissions at weight 10 or contact-us at weight 8)
        self.assertNotEqual(fetcher.fetched[1], HOME_URL)


# ---------------------------------------------------------------------------
# Regression: select_priority_pages canonical fallback path
# ---------------------------------------------------------------------------

NO_KEYWORD_HTML = """\
<html><body>
<h1>KXYZ</h1>
<a href="/about">About us</a>
<a href="/history">Our history</a>
<a href="https://external.example/foo">External</a>
</body></html>
"""

NO_KEYWORD_ABOUT_HTML = """\
<html><body><h1>About KXYZ</h1><p>Community station.</p></body></html>
"""

NO_KEYWORD_HISTORY_HTML = """\
<html><body><h1>History</h1><p>Founded 1990.</p></body></html>
"""


class TestSelectPriorityPagesFallback(unittest.TestCase):
    """Regression: homepage with no keyword-matching links still yields
    bounded conventional fallback pages (GUESSED_PATHS)."""

    def _engine(self, budget=6):
        return EnrichmentEngine(config=type("C", (), {
            "max_pages_per_station": budget,
            "timeout_seconds": 5.0,
            "rate_limit_seconds": 0.0,
            "respect_robots": False,
            "user_agent": "test",
            "logger": None,
        })())

    def test_no_keyword_links_yields_fallback_pages(self):
        """A homepage with zero keyword-matching links should still
        produce conventional fallback pages like /contact."""
        engine = self._engine(budget=6)
        page = parse_html(HOME_URL, NO_KEYWORD_HTML)
        result = engine._discover_internal_pages(
            [page], [HOME_URL], 6)
        # Should get fallback pages (e.g. /contact, /submissions)
        self.assertGreater(len(result), 0)
        # All should be same-site
        for url in result:
            self.assertIn("kzzz.example", url)

    def test_fallback_respects_budget(self):
        """Even with generous budget, fallback pages are bounded."""
        engine = self._engine(budget=2)
        page = parse_html(HOME_URL, NO_KEYWORD_HTML)
        result = engine._discover_internal_pages(
            [page], [HOME_URL], 2)
        self.assertLessEqual(len(result), 2)

    def test_keyword_links_preferred_over_fallback(self):
        """When keyword-matching links exist, they appear before
        conventional fallback pages."""
        engine = self._engine(budget=10)
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        result = engine._discover_internal_pages(
            [page], [HOME_URL], 10)
        # submissions (weight 10) should be first
        self.assertTrue(
            any("submissions" in u for u in result[:2]),
            f"Expected submissions in top 2, got {result[:2]}")

    def test_budget_never_exceeded(self):
        """Total discovered pages never exceeds the remaining budget."""
        for budget in (1, 2, 3, 5, 10):
            engine = self._engine(budget=budget)
            page = parse_html(HOME_URL, HOMEPAGE_HTML)
            result = engine._discover_internal_pages(
                [page], [HOME_URL], budget)
            self.assertLessEqual(
                len(result), budget,
                f"Budget {budget} exceeded: got {len(result)} pages")

    def test_already_fetched_urls_not_in_result(self):
        """URLs already fetched are never returned by discovery."""
        engine = self._engine(budget=10)
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        already = [HOME_URL, STAFF_URL, CONTACT_URL, SUBMISSIONS_URL]
        result = engine._discover_internal_pages(
            [page], already, 10)
        already_normalized = set()
        for u in already:
            from crawler.urls import normalize_url
            try:
                already_normalized.add(normalize_url(u))
            except ValueError:
                pass
        for url in result:
            from crawler.urls import normalize_url as _n
            self.assertNotIn(
                _n(url), already_normalized,
                f"Already-fetched URL returned: {url}")

    def test_dedup_across_multiple_pages(self):
        """Links found on multiple pages are not duplicated."""
        engine = self._engine(budget=10)
        page1 = parse_html(HOME_URL, HOMEPAGE_HTML)
        page2_html = """\
<html><body>
<a href="/staff">Staff</a>
<a href="/contact-us">Contact</a>
<a href="/new-page">New</a>
</body></html>
"""
        page2 = parse_html(f"{BASE}/some-page", page2_html)
        result = engine._discover_internal_pages(
            [page1, page2], [HOME_URL], 10)
        self.assertEqual(len(result), len(set(result)),
                         f"Duplicates found: {result}")


if __name__ == "__main__":
    unittest.main()
