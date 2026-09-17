"""Phase 10 — intelligence-repair verification tests.

Deterministic coverage of the 12 mandated behaviors after the intelligence
repairs (contact quality, useful-page accuracy, reachability, enrichment
stability, outreach honesty, draft integrity). No network access: every fetch
goes through injected fake fetchers.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from crawler.http import FetchResult
from crawler.pages import parse_html
from crawler.urls import is_individual_contact_route

from discovery.radio.enrich_pipeline import EnrichmentEngine
from discovery.radio.intelligence import (
    _SUBMISSION_ROLE_RANK,
    build_intelligence_record,
    classify_useful_page,
)
from discovery.radio.schema import SourceFetchRecord

from enrichment.confidence import score_contact
from enrichment.staff_directory import _looks_like_person_name, \
    is_staff_directory

from database.service import PersistenceService, normalize_intelligence_record

from outreach.service import create_outreach

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DRAFT_GEN = FRONTEND / "js" / "draftGenerator.js"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

HOME_URL = "https://kzjz.example/"
SUBMIT_URL = "https://kzjz.example/send-music"
CONTACT_URL = "https://kzjz.example/contact"
STAFF_URL = "https://kzjz.example/staff"
DJ_INDEX_URL = "https://kzjz.example/email.php"
DJ_MAIL_URL = "https://kzjz.example/email.php?id=42"
GOOD_SUBMIT_HTML = (
    "<html><title>Send Us Your Music</title><body>"
    "<h1>Send music</h1><p>We accept demos by email to "
    "submit@kzjz.example. No phone calls.</p></body></html>"
)
GENERIC_CONTACT_HTML = (
    "<html><title>Contact Us</title><body>"
    "<h1>Contact</h1><p>Office hours 9-5. Phone 555-0100.</p>"
    "<p>Address: 1 Radio Lane.</p></body></html>"
)
HOMEPAGE_HTML = (
    "<html><title>KZJZ Radio</title><body>"
    '<a href="/send-music">Send Us Music</a>'
    '<a href="/contact">Contact Us</a>'
    '<a href="/staff">Our Staff</a>'
    '<a href="email.php?id=42">Email DJ</a>'
    "</body></html>"
)
STAFF_HTML = (
    "<html><title>Staff</title><body>"
    "<h1>Our Team</h1>"
    "<p>Music Director: Jane Jones, jane@kzjz.example</p>"
    "<p>Webmaster: Bob Smith, webmaster@kzjz.example</p>"
    "</body></html>"
)
DJ_INDEX_HTML = (
    "<html><title>Email Our DJs</title><body>"
    '<p><a href="email.php?id=1">DJ Alex</a></p>'
    '<p><a href="email.php?id=2">DJ Beth</a></p>'
    "</body></html>"
)


class FakeFetcher:
    """Exact-URL fetcher double; optional failure/redirect simulation."""

    def __init__(self, pages=None, fail_urls=None, redirects=None):
        self.pages = dict(pages or {})
        self.fail_urls = set(fail_urls or [])
        self.redirects = dict(redirects or {})
        self.fetched = []

    def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        if url in self.fail_urls:
            return FetchResult(url=url, error_kind="http_status",
                               error_message="boom")
        if url in self.redirects:
            target = self.redirects[url]
            if target in self.pages:
                return FetchResult(url=url, final_url=target, status=301,
                                   content_type="text/html",
                                   body=self.pages[target])
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url=url, error_kind="dns_error",
                               error_message="fixture miss")
        return FetchResult(url=url, final_url=url, status=200,
                           content_type="text/html", body=body)


def station(**overrides) -> dict:
    record = {
        "id": "phase10-station",
        "name": "KZJZ 97.7 FM",
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
        "raw_metadata": {},
    }
    record.update(overrides)
    return record


def fetch_records(urls, *, ok=True, status=200):
    return [SourceFetchRecord(url=u, ok=ok, status=status) for u in urls]


def make_engine(pages, *, fail_urls=None, redirects=None,
                max_pages=4, verify=8):
    fetcher = FakeFetcher(pages=pages, fail_urls=fail_urls,
                          redirects=redirects)
    engine = EnrichmentEngine(fetcher=fetcher)
    engine.config.max_pages_per_station = max_pages
    engine.config.verify_pages_per_station = verify
    return engine


# ---------------------------------------------------------------------------
# 1. valid music submission page -> classified send-like, exact URL kept
# ---------------------------------------------------------------------------

class TestSubmissionPage(unittest.TestCase):
    def test_send_music_page_keeps_exact_submission_url(self):
        page = parse_html(SUBMIT_URL, GOOD_SUBMIT_HTML)
        record = build_intelligence_record(
            station(), [page], fetch_records([SUBMIT_URL]))
        self.assertIsNotNone(record.submission)
        self.assertIsNotNone(record.submission.submission_email)
        self.assertEqual(record.submission.submission_email,
                         "submit@kzjz.example")
        self.assertIn("We accept demos",
                      record.submission.instructions["value"])
        self.assertEqual(
            classify_useful_page("Send Us Music", SUBMIT_URL), "send_music")

    def test_useful_page_carrying_submission_url_preserved_exactly(self):
        home = parse_html(HOME_URL, HOMEPAGE_HTML)
        submit = parse_html(SUBMIT_URL, GOOD_SUBMIT_HTML)
        record = build_intelligence_record(
            station(submission_url={"value": SUBMIT_URL,
                                    "source_url": HOME_URL,
                                    "source_type": "link_anchor_rule",
                                    "method": "anchor_rule",
                                    "discovered_at": "",
                                    "also_seen_at": []}),
            [home, submit], fetch_records([HOME_URL, SUBMIT_URL]))
        self.assertIsNotNone(record.submission)
        # The EXACT discovered href survives end-to-end, never re-guessed.
        self.assertEqual(record.submission.submission_url["value"], SUBMIT_URL)
        self.assertEqual(record.submission.submission_email,
                         "submit@kzjz.example")


# ---------------------------------------------------------------------------
# 2. generic contact page is never labeled a music submission
# ---------------------------------------------------------------------------

class TestGenericContactPage(unittest.TestCase):
    def test_plain_contact_page_is_not_send_music(self):
        page = parse_html(CONTACT_URL, GENERIC_CONTACT_HTML)
        record = build_intelligence_record(
            station(), [page], fetch_records([CONTACT_URL]))
        self.assertNotEqual(classify_useful_page("Contact Us", CONTACT_URL),
                            "send_music")
        if record.submission is not None:
            self.assertIsNone(record.submission.submission_email)
            self.assertIsNone(record.submission.submission_url)


# ---------------------------------------------------------------------------
# 3. individual verified music-relevant contact
# ---------------------------------------------------------------------------

class TestVerifiedMusicContact(unittest.TestCase):
    def test_music_director_with_email_is_high_confidence(self):
        from discovery.radio.intelligence import _is_qualified_contact
        raw = {
            "name": "Jane Jones", "role": "music_director",
            "email": "jane@kzjz.example",
            "source_url": STAFF_URL,
        }
        self.assertTrue(_is_qualified_contact(raw))
        contact = {
            "name": "Jane Jones", "role": "music_director",
            "email": "jane@kzjz.example", "source_url": STAFF_URL,
        }
        score, reasons = score_contact(contact, {"kzjz.example"})
        self.assertGreater(score, 0.5)
        self.assertTrue(any("music" in r.lower() for r in reasons))


# ---------------------------------------------------------------------------
# 4. generic staff member is never a music decision-maker
# ---------------------------------------------------------------------------

class TestGenericStaffRejected(unittest.TestCase):
    def test_webmaster_not_promoted_to_submission_decider(self):
        page = parse_html(STAFF_URL, STAFF_HTML)
        record = build_intelligence_record(station(), [page])
        self.assertNotIn("unknown", _SUBMISSION_ROLE_RANK)
        self.assertNotIn("advertising", _SUBMISSION_ROLE_RANK)
        roles = {c.role for c in record.contacts}
        music_people = [
            c for c in record.contacts if c.role in _SUBMISSION_ROLE_RANK]
        # The music director is surfaced and preferred; Bob the webmaster is
        # never in the role ranks at all, and neither is a generic label.
        self.assertIn("music_director", roles)
        self.assertTrue(all(
            c.preferred_for_submissions for c in music_people))
        for contact in record.contacts:
            if contact.email and "webmaster@" in contact.email:
                self.assertNotIn(contact.role, _SUBMISSION_ROLE_RANK)


# ---------------------------------------------------------------------------
# 5. false-positive contact candidates rejected
# ---------------------------------------------------------------------------

class TestFalsePositiveRejected(unittest.TestCase):
    def test_navigation_labels_rejected_as_people(self):
        for junk in ("Send Us Music", "Stream Help", "Advanced Search",
                     "Volunteer Page."):
            self.assertFalse(_looks_like_person_name(junk))

    def test_individual_email_route_never_surfaces_as_staff_directory(self):
        # A per-DJ email form (email.php?id=N) is PEOPLE, not a station page:
        # it is flagged as an individual-contact route and excluded from staff
        # directory extraction. The collection index (email.php without an id)
        # is the station page and stays.
        page = parse_html(DJ_MAIL_URL, DJ_INDEX_HTML)
        self.assertTrue(is_individual_contact_route(DJ_MAIL_URL))
        self.assertFalse(is_staff_directory(page))
        self.assertFalse(is_individual_contact_route(DJ_INDEX_URL))


# ---------------------------------------------------------------------------
# 6. exact useful page URL preserved (never constructed from a label)
# ---------------------------------------------------------------------------

class TestExactUsefulPageUrl(unittest.TestCase):
    def test_exact_discovered_href_preserved(self):
        page = parse_html(HOME_URL, HOMEPAGE_HTML)
        record = build_intelligence_record(
            station(), [page], fetch_records(
                [HOME_URL, SUBMIT_URL, CONTACT_URL, STAFF_URL]))
        urls = {p.url for p in record.useful_pages}
        self.assertIn(SUBMIT_URL, urls)
        self.assertIn(CONTACT_URL, urls)
        for u in urls:
            self.assertTrue(u.startswith("https://kzjz.example/"))


# ---------------------------------------------------------------------------
# 7. dead useful-page URL marked unreachable after verification
# ---------------------------------------------------------------------------

class TestDeadUsefulPage(unittest.TestCase):
    def test_unfetchable_useful_pages_are_marked_unreachable(self):
        engine = make_engine({HOME_URL: HOMEPAGE_HTML}, max_pages=3,
                             verify=8)
        enriched = engine.enrich_records([station()]).records[0]
        dead = [p for p in enriched["useful_pages"]
                if p["url"] not in engine._fetcher.pages]
        self.assertGreater(len(dead), 0)
        for p in dead:
            self.assertIs(p["reachable"], False)


# ---------------------------------------------------------------------------
# 8. reachable domain with a failed route
# ---------------------------------------------------------------------------

class TestReachableDomainFailedRoute(unittest.TestCase):
    def test_route_failure_recorded_honestly_with_evidence(self):
        engine = make_engine(
            {HOME_URL: HOMEPAGE_HTML, SUBMIT_URL: GOOD_SUBMIT_HTML,
             CONTACT_URL: GENERIC_CONTACT_HTML},
            fail_urls=[STAFF_URL], max_pages=4, verify=8)
        enriched = engine.enrich_records([station()]).records[0]
        staff = [p for p in enriched["useful_pages"]
                 if p["url"] == STAFF_URL]
        self.assertTrue(staff)
        self.assertFalse(staff[0]["reachable"])
        self.assertTrue(staff[0]["rechecked_at"])


# ---------------------------------------------------------------------------
# 9. successful redirect resolution recorded as verified
# ---------------------------------------------------------------------------

class TestRedirectResolution(unittest.TestCase):
    def test_redirected_submission_url_resolves_and_is_ok(self):
        redirect_url = "https://kzjz.example/submit-new"
        engine = make_engine(
            {HOME_URL: HOMEPAGE_HTML, SUBMIT_URL: GOOD_SUBMIT_HTML},
            redirects={redirect_url: SUBMIT_URL}, max_pages=4, verify=8)
        rec = station()
        rec["submission_url"] = {
            "value": redirect_url, "source_url": HOME_URL,
            "source_type": "link_anchor_rule", "method": "anchor_rule",
            "discovered_at": "", "also_seen_at": []}
        enriched = engine.enrich_records([rec]).records[0]
        fetch = next(f for f in enriched["fetches"]
                     if f["url"] == redirect_url)
        self.assertTrue(fetch["ok"])
        self.assertIn(SUBMIT_URL, {f["url"] for f in enriched["fetches"]})


# ---------------------------------------------------------------------------
# 10. enrichment failure recorded honestly; one bad station never kills run
# ---------------------------------------------------------------------------

class TestEnrichmentFailure(unittest.TestCase):
    def test_dead_site_recorded_honestly_never_confidently(self):
        class SelectiveBoom(FakeFetcher):
            def fetch(self, url):
                if "boom-b.example" in url:
                    raise RuntimeError("simulated outage")
                return super().fetch(url)

        fetcher = SelectiveBoom(
            pages={HOME_URL: HOMEPAGE_HTML,
                   "https://boom-b.example/": "<html><title>down</title>"})
        engine = EnrichmentEngine(fetcher=fetcher)
        engine.config.verify_pages_per_station = 4
        result = engine.enrich_records([
            station(),
            station(name="KZJZ-b", website="https://boom-b.example/",
                    source_urls=[]),
        ])
        # The run completes with both stations represented...
        self.assertEqual(len(result.records), 2)
        # ...but the dead site's every fetch is honestly marked not-ok.
        boom = next(r for r in result.records
                    if r["website"] == "https://boom-b.example/")
        self.assertTrue(boom["fetches"])
        self.assertTrue(all(not f["ok"] for f in boom["fetches"]))

    def test_catastrophic_record_failure_never_kills_the_run(self):
        engine = EnrichmentEngine(
            fetcher=FakeFetcher(pages={HOME_URL: HOMEPAGE_HTML}))
        result = engine.enrich_records(["not-a-dict"])  # malformed record
        self.assertEqual(result.failure_count, 1)
        self.assertEqual(result.failures[0].stage, "enrichment")
        self.assertIn("error_kind", vars(result.failures[0]))
        # A subsequent healthy record still enriches.
        result = engine.enrich_records([station()])
        self.assertEqual(result.record_count, 1)
        self.assertEqual(result.failure_count, 0)


# ---------------------------------------------------------------------------
# 11. outreach recipient rejected when evidence is insufficient
# ---------------------------------------------------------------------------

class TestOutreachRecipientRejection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.addCleanup(self.storage.close)

    def test_missing_recipient_route_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_outreach(self.storage, payload={})
        self.assertIn("outreach_requires_recipient", str(ctx.exception))

    def test_bogus_url_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_outreach(self.storage, payload={"recipient": {
                "submission_url": "not-a-url", "identity_key": "domain:x"}})
        self.assertIn("outreach_requires_verified_url", str(ctx.exception))

    def test_both_email_and_url_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_outreach(self.storage, payload={"recipient": {
                "email": "a@b.example",
                "submission_url": "https://x.example/y"}})
        self.assertIn("outreach_ambiguous_recipient", str(ctx.exception))


# ---------------------------------------------------------------------------
# webform outreach route (URL-only stations selectable, email never fabricated)
# ---------------------------------------------------------------------------

class TestWebformOutreach(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.addCleanup(self.storage.close)

    def test_webform_outreach_uses_verified_url_with_empty_email(self):
        record = create_outreach(self.storage, payload={
            "recipient": {
                "contact_uid": "wf_https",
                "identity_key": "domain:wfmu.example",
                "name": "Station Submission Page",
                "role": "musical_submission",
                "organization": "WFMU",
                "outreach_class": "webform",
                "submission_url": "https://wfmu.example/sendmusic",
                "source_url": "https://wfmu.example/",
            },
            "subject": "Demo submission",
            "message": "Hi, please consider this track.",
        })
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["recipient"]["outreach_class"], "webform")
        self.assertEqual(record["recipient"]["submission_url"],
                         "https://wfmu.example/sendmusic")
        # Nothing is ever fabricated: webform recipients carry NO email.
        self.assertEqual(record["recipient"]["email"], "")
        again = self.storage.get_outreach(record["outreach_id"])
        self.assertEqual(again["outreach_class"], "webform")
        self.assertEqual(again["submission_url"],
                         "https://wfmu.example/sendmusic")


# ---------------------------------------------------------------------------
# contacts full-replace reconciliation (stale entries removed on re-enrich)
# ---------------------------------------------------------------------------

class TestFullReplaceReconciliation(unittest.TestCase):
    def test_stale_contacts_are_deleted_on_reingest(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        storage = PersistenceService(
            os.path.join(self._tmp.name, "db.sqlite"))
        self.addCleanup(storage.close)
        rec = station()
        identity = normalize_intelligence_record(rec)[1]
        rec["contacts"] = [
            {"name": "Jane Jones", "role": "music_director",
             "email": "jane@kzjz.example", "source_url": STAFF_URL},
            {"name": "DJ Garbage", "role": "dj", "source_url": DJ_INDEX_URL},
        ]
        storage.ingest_intelligence([rec], source="test")
        self.assertEqual(len(storage.get_station_contacts(identity)), 2)
        fresh = dict(rec)
        fresh["contacts"] = [
            {"name": "Jane Jones", "role": "music_director",
             "email": "jane@kzjz.example", "source_url": STAFF_URL},
        ]
        storage.ingest_intelligence([fresh], source="test")
        names = {c["name"] for c in storage.get_station_contacts(identity)}
        self.assertEqual(names, {"Jane Jones"})


# ---------------------------------------------------------------------------
# 12. draft generation uses verified fields only (deterministic)
# ---------------------------------------------------------------------------

class TestDraftGeneration(unittest.TestCase):
    def test_draft_is_deterministic_and_uses_verified_fields_only(self):
        if not DRAFT_GEN.exists():
            self.skipTest("draftGenerator.js missing")
        import pathlib
        draft_uri = pathlib.Path(DRAFT_GEN).resolve().as_uri()
        src = (
            "import('" + draft_uri + "')\n"
            ".then(({ generateDraft }) => {\n"
            "  const base = {\n"
            "    name: 'Jane Jones', role: 'music_director',\n"
            "    organization: 'KZJZ', email: 'jane@kzjz.example' };\n"
            "  const track = {\n"
            "    title: 'Neon Sky', listen_url: 'https://s3.example/x.mp3' };\n"
            "  const artist = { name: 'Starfire' };\n"
            "  const a = generateDraft(base, track, artist);\n"
            "  const b = generateDraft(base, track, artist);\n"
            "  const missing = generateDraft(\n"
            "    { name: 'Jane Jones', role: 'music_director',\n"
            "      organization: 'KZJZ' }, {}, {});\n"
            "  console.log(JSON.stringify({ a, b, missing }));\n"
            "})\n"
        )
        proc = subprocess.run(["node", "--input-type=module", "--eval", src],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["a"], payload["b"])
        self.assertIn("Jane Jones", payload["a"]["message"])
        self.assertIn("Neon Sky", payload["a"]["subject"])
        self.assertIn("https://s3.example/x.mp3", payload["a"]["message"])
        # No invented listen link when none was provided.
        self.assertNotIn("http", payload["missing"]["message"])
        self.assertIn("New music submission", payload["missing"]["subject"])


if __name__ == "__main__":
    unittest.main()