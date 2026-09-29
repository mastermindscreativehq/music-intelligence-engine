"""Station qualification must describe a STATION, not a page about one.

Regression tests for the forensic finding that a news/interview/blog page
which merely MENTIONS a real radio station was being persisted as a
``radio_station`` record and shown on the Stations dashboard.

Both gates are covered:

- ``classify_station_candidate`` (pre-fetch, no network): article,
  compound (``news-events``, ``news-info``) and locale-prefixed
  (``/en-int/about/news-info/``) per-item paths are rejected before any
  fetch.
- ``classify_station_site`` (post-fetch, the HARD gate): page-type
  precedence runs BEFORE station-evidence scanning, and station evidence
  is only accepted on the site's own entry point together with a
  self-reference — so quoted article prose can never qualify a station.

The genuine-station cases are asserted alongside every rejection so a
future tightening cannot silently reject real stations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from backend.routes import dispatch

from database.service import PersistenceService

from discovery.radio.qualify import (
    NEEDS_REVIEW,
    QUALIFIED,
    REJECTED,
    article_path_segment,
    classify_station_candidate,
    classify_station_site,
)

TOKEN = "MIE-TEST-TOKEN"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# ---------------------------------------------------------------------------
# The two known production false positives (verbatim from the audit).
# ---------------------------------------------------------------------------

UMN_URL = ("https://twin-cities.umn.edu/news-events/"
           "real-college-radio-real-opportunity-students")
UMN_TITLE = ("\u2018Real College Radio\u2019 is a real opportunity for "
             "students | University of Minnesota")
# The article body quotes a real station: KCPR 91.7 FM. Every station
# signal fires on this text, yet the site is a university news page.
UMN_ARTICLE_TEXT = (
    "KCPR 91.7 FM college radio broadcasting from the University of "
    "Minnesota Twin Cities. Student radio is a real opportunity. "
    "We broadcast on FM. college radio station. To submit music to KCPR, "
    "listen live and now on air.")

RODE_URL = ("https://rode.com/en-int/about/news-info/"
            "they-killed-local-radio-podcasting-was-a-response-to-that-"
            "interview-with-mike-dawson")
RODE_TITLE = ('"They Killed Local Radio. Podcasting Was a Response to That": '
              "Interview With Mike Dawson (US)")
RODE_ARTICLE_TEXT = (
    "Independent radio podcast interview. 90.7 FM community radio mention. "
    "station_type college radio. We broadcast live on 100.1 FM. "
    "listen live now on air.")


class KnownFalsePositiveTests(unittest.TestCase):
    """The audit's two named records must never become stations."""

    def test_umn_article_is_not_a_station(self):
        pre = classify_station_candidate(
            url=UMN_URL, title=UMN_TITLE, snippet=UMN_ARTICLE_TEXT[:120])
        self.assertEqual(pre.verdict, REJECTED)
        self.assertEqual(pre.kind, "non_station_path")

        post = classify_station_site(
            website_url=UMN_URL, homepage_title=UMN_TITLE,
            texts=(UMN_ARTICLE_TEXT,))
        self.assertEqual(post.verdict, REJECTED)
        self.assertEqual(post.kind, "non_station_path")
        self.assertIn("article", post.reason)

    def test_rode_interview_is_not_a_station(self):
        pre = classify_station_candidate(
            url=RODE_URL, title=RODE_TITLE, snippet=RODE_ARTICLE_TEXT[:120])
        self.assertEqual(pre.verdict, REJECTED)

        post = classify_station_site(
            website_url=RODE_URL, homepage_title=RODE_TITLE,
            texts=(RODE_ARTICLE_TEXT,))
        self.assertEqual(post.verdict, REJECTED)
        self.assertEqual(post.kind, "non_station_path")

    def test_article_prose_never_qualifies_even_on_a_root_path(self):
        # Even with a clean entry-point path, a page that only REPORTS on a
        # real station in the third person is not self-identification. This
        # is the real-world failure mode: newsroom and directory prose naming
        # a genuine callsign, which used to be enough to qualify.
        post = classify_station_site(
            website_url="https://newsroom.example.org/local-coverage",
            homepage_title="How Community Radio Is Changing Music",
            texts=("KQXR 91.5 FM is a college radio station that broadcasts "
                   "from Cedar Valley. The station has been on the air since "
                   "1984 and streams its schedule online.",))
        self.assertNotEqual(post.verdict, QUALIFIED)
        self.assertEqual(post.verdict, NEEDS_REVIEW)
        self.assertEqual(post.kind, "mentions_station")

    def test_first_person_station_homepage_qualifies_without_a_branded_title(self):
        # The primary page is often an interior page whose title carries no
        # callsign ("Music Submissions - Radio Laurier"). First-person
        # self-identification is the load-bearing gate, so these real stations
        # must NOT be demoted by a title-branding requirement.
        post = classify_station_site(
            website_url="https://kzyx.example.org/music-submissions",
            homepage_title="How to Submit Music",
            texts=("KZYX is a community radio station. Our station broadcasts "
                   "90.7 FM from Philomath. Listen live.",))
        self.assertEqual(post.verdict, QUALIFIED)

    def test_sub_page_station_mention_cannot_promote_the_entry_point(self):
        # Requirement: evidence must be on the site's own entry point.
        # All decisive signals live only in a linked sub-page.
        post = classify_station_site(
            website_url="https://kqxr.example.org/",
            homepage_title="Community Media Collective",
            texts=("Welcome to our community media project.",),
            secondary_texts=(
                "Our frequency is 101.5 FM. We broadcast from Cedar Valley.",))
        self.assertNotEqual(post.verdict, QUALIFIED)
        self.assertEqual(post.verdict, NEEDS_REVIEW)
        # The sub-page evidence is still reported for a human reviewer.
        self.assertTrue(any(e.startswith("subpage:")
                            for e in post.evidence), post.evidence)


class CompoundAndLocaleArticlePathTests(unittest.TestCase):
    """Compound + locale-prefixed per-item paths are recognized."""

    def test_compound_news_segments_are_article_pages(self):
        for path in ("/news-events/real-college-radio",
                     "/news-info/killed-local-radio",
                     "/news_events/item",
                     "/news-events-archive/2026/story",
                     "/blog/post", "/blogs/music-marketing",
                     "/posts/2024/interview", "/interview/mike-dawson",
                     "/articles/10.3389/comm", "/article/one",
                     "/stories/local", "/press/2026/launch",
                     "/podcast/episode-4"):
            with self.subTest(path=path):
                self.assertIsNotNone(
                    article_path_segment(f"https://host.example{path}"))

    def test_locale_prefixed_article_paths_are_article_pages(self):
        # Locale-prefixed forms of the same article roots must still match:
        # the segment scan is position-independent.
        for path in ("/en-int/about/news-info/they-killed-local-radio",
                     "/en-us/about/news-info/all",
                     "/en-gb/blog/featured-creator",
                     "/en-us/news-events/studio-opening",
                     "/fr-en/about/interview/mike-dawson"):
            with self.subTest(path=path):
                self.assertIsNotNone(
                    article_path_segment(f"https://host.example{path}"))

    def test_article_paths_are_rejected_pre_fetch(self):
        for path in ("/news-events/real-college-radio",
                     "/en-int/about/news-info/interview",
                     "/blogs/how-to-get-played",
                     "/articles/10.3389/full",
                     "/posts/2024/launch"):
            with self.subTest(path=path):
                verdict = classify_station_candidate(
                    url=f"https://host.example{path}",
                    title="Real College Radio | University of Minnesota")
                self.assertEqual(verdict.verdict, REJECTED)
                self.assertEqual(verdict.kind, "non_station_path")

    def test_article_paths_are_rejected_post_fetch(self):
        post = classify_station_site(
            website_url="https://host.example/en-int/about/news-info/x",
            homepage_title="They Killed Local Radio",
            texts=("We broadcast live on 100.1 FM. Listen live now on air.",))
        self.assertEqual(post.verdict, REJECTED)

    def test_station_entry_routes_are_still_allowed(self):
        # Tightening must not reject a real station's shallow entry routes.
        for path in ("/", "/radio", "/fm", "/listen", "/about",
                     "/contact", "/submissions", "/shows", "/schedule",
                     "/en-us"):
            with self.subTest(path=path):
                self.assertIsNone(
                    article_path_segment(f"https://station.example{path}"))
                self.assertNotEqual(
                    classify_station_candidate(
                        url=f"https://station.example{path}",
                        title="KQXR Community Radio").verdict,
                    REJECTED)


class SelfIdentificationTests(unittest.TestCase):
    """Station self-identification is required, and it is sufficient."""

    def test_genuine_station_homepage_still_qualifies(self):
        post = classify_station_site(
            website_url="https://kqxr.example.org/",
            homepage_title="KQXR 101.5 FM | Community Radio for Cedar Valley",
            texts=("Community radio, listener-supported and independently "
                   "operated, broadcasting from Cedar Valley. "
                   "General inquiries: info@kqxr.example",))
        self.assertEqual(post.verdict, QUALIFIED)
        self.assertIn("callsign", post.evidence)
        self.assertIn("self_reference", post.evidence)

    def test_genuine_station_qualifies_via_our_frequency(self):
        post = classify_station_site(
            website_url="https://kunc.example/",
            homepage_title="KUNC Community Radio",
            texts=("Our frequency is 88.3 FM. Listen live to KUNC.",))
        self.assertEqual(post.verdict, QUALIFIED)

    def test_bare_frequency_without_self_reference_is_not_qualified(self):
        post = classify_station_site(
            website_url="https://kunc.example/",
            homepage_title="KUNC Community Radio",
            texts=("The station broadcasts on 88.3 FM.",))
        self.assertEqual(post.verdict, NEEDS_REVIEW)
        self.assertEqual(post.kind, "mentions_station")

    def test_bare_station_type_without_self_reference_is_not_qualified(self):
        post = classify_station_site(
            website_url="https://blog.example/",
            homepage_title="A college radio wrap-up",
            texts=("Independent radio is having a moment.",))
        self.assertNotEqual(post.verdict, QUALIFIED)

    def test_denied_host_still_rejected(self):
        post = classify_station_site(
            website_url="https://www.npr.org/sections/allsongs/",
            homepage_title="NPR",
            texts=("Our frequency is 91.5 FM. We broadcast live.",))
        self.assertEqual(post.verdict, REJECTED)
        self.assertEqual(post.kind, "denied_host")

    def test_platform_host_still_rejected(self):
        post = classify_station_site(
            website_url="https://www.youtube.com/watch?v=abc",
            homepage_title="Radio stream",
            texts=("Our frequency is 88.3 FM. Listen live.",))
        self.assertEqual(post.verdict, REJECTED)
        self.assertEqual(post.kind, "platform_page")

    def test_information_page_still_rejected(self):
        post = classify_station_site(
            website_url="https://school.example/",
            homepage_title="Radio school",
            texts=("How to start a radio station, explained.",))
        self.assertEqual(post.verdict, REJECTED)
        self.assertEqual(post.kind, "non_station_info")

    def test_reachable_site_with_no_evidence_is_needs_review(self):
        post = classify_station_site(
            website_url="https://unknown.example/",
            homepage_title="Welcome", texts=("Nothing relevant here.",))
        self.assertEqual(post.verdict, NEEDS_REVIEW)
        self.assertEqual(post.kind, "no_station_evidence")


class QuarantineListingApiTests(unittest.TestCase):
    """?status= must never re-admit quarantined stations."""

    # NOTE: real-looking TLDs on purpose — ``.example``/``.test`` hosts are
    # dev-fixture quarantined by the read filter, which would make this test
    # pass for the wrong reason.
    QUALIFIED_KEY = "domain:kqxr-radio.org"
    NEEDS_REVIEW_KEY = "domain:radio-mentions.com"
    REJECTED_KEY = "domain:news-gov.net"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = PersistenceService(
            os.path.join(self._tmp.name, "mie.db"))
        self._store(key=self.QUALIFIED_KEY, name="KQXR Radio",
                    verdict="qualified")
        self._store(key=self.NEEDS_REVIEW_KEY, name="Station Mentions",
                    verdict="needs_review")
        self._store(key=self.REJECTED_KEY, name="Denied Host Station",
                    verdict="rejected")

    def tearDown(self):
        self.repo.close()
        self._tmp.cleanup()

    def _store(self, *, key: str, name: str, verdict: str) -> None:
        self.repo.ingest_intelligence([{
            "identity_key": key,
            "name": name,
            "organization_type": "radio_station",
            "website": f"https://{key.split(':', 1)[1]}/",
            "status": "enriched",
            "raw_metadata": {
                "qualification": {"verdict": verdict, "kind": "test",
                                  "reason": "test", "evidence": []},
            },
        }], source="qualification_regression_test")

    def _list(self, query: str = "") -> dict:
        status, body = dispatch(
            self.repo, "GET", "/api/v1/stations", _query(query), None,
            headers=AUTH)
        self.assertEqual(status, 200)
        return body["data"]

    def test_unfiltered_listing_hides_quarantined(self):
        names = {row["name"] for row in self._list()["stations"]}
        self.assertIn("KQXR Radio", names)
        self.assertNotIn("Station Mentions", names)
        self.assertNotIn("Denied Host Station", names)

    def test_status_filter_cannot_expose_quarantined(self):
        data = self._list("?status=enriched")
        names = {row["name"] for row in data["stations"]}
        self.assertIn("KQXR Radio", names)
        self.assertNotIn("Station Mentions", names)
        self.assertNotIn("Denied Host Station", names)

    def test_status_filter_reports_quarantined_excluded(self):
        data = self._list("?status=enriched")
        self.assertEqual(data["quarantined_excluded"], 2)
        self.assertEqual(data["total"], 1)

    def test_quarantined_rows_are_still_stored_for_review(self):
        # Quarantine is a READ filter; storage is never mutated.
        self.assertIsNotNone(self.repo.get_station(self.NEEDS_REVIEW_KEY))
        self.assertIsNotNone(self.repo.get_station(self.REJECTED_KEY))


def _query(query: str) -> dict:
    """Parse a raw query string into the params map dispatch expects."""
    params: dict[str, list[str]] = {}
    if not query:
        return params
    for part in query.lstrip("?").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        params.setdefault(key, []).append(value)
    return params


if __name__ == "__main__":
    unittest.main()
