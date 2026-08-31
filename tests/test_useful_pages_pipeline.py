"""Full-chain regression for evidence-backed station Useful Pages and action links.

Covers the SINGLE canonical source rule end-to-end across many station
website structures — no station-specific hardcoding, no invented routes:

    crawl evidence (HTML anchors)
      → ParsedPage.links (label + exact resolved href + source page)
      → build_intelligence_record (UsefulPage list, persisted in raw_metadata)
      → storage ingest + retrieval
      → intelligence_payload (API contract surfaces ``useful_pages``)
      → frontend contract (Send Music / Useful Pages read ONLY useful_pages)

The station structures deliberately differ (static HTML, subpage-relative
URLs, mailto/off-site noise, an SPA that exposes no navigable anchors, a
submission URL only reachable from a nested page). Real station domains are
used only as *fixture* hostnames; the implementation must never hardcode them.

No network. No credentials.
"""

import os
import tempfile
import unittest
from pathlib import Path

from crawler.pages import parse_html

from discovery.radio.intelligence import build_intelligence_record
from discovery.radio.schema import SourceFetchRecord

from database.service import PersistenceService, normalize_intelligence_record

from backend.contracts import intelligence_payload


def station_dict(name, website, **overrides):
    record = {
        "id": "station-" + name.replace(" ", "-").lower(),
        "organization_type": "radio_station",
        "name": name,
        "website": website,
        "domain": website.split("//")[1].split("/")[0],
        "station_type": "public",
        "classification_confidence": 0.8,
        "classification_evidence": ["public radio"],
        "formats": [], "genres": [],
        "emails": [], "phone_numbers": [], "contacts": [],
        "source_urls": [website],
        "website_reachable": True,
        "discovered_at": "2026-08-30T00:00:00+00:00",
        "last_observed_at": "2026-08-30T00:00:00+00:00",
        "status": "discovered",
        "raw_metadata": {},
    }
    record.update(overrides)
    return record


def pages(*html):
    return [parse_html(url, body) for url, body in html]


HOME_WS = "https://static.example/"
HOME_SPA = "https://spa.example/"
HOME_NESTED = "https://nested.example/"
HOME_NOISE = "https://noise.example/"


# Structure A: static HTML — submission + DJ directory links on the homepage.
STATIC_HOME = """
<html><body>
  <h1>Static Radio</h1>
  <a href="/music/send">Send Us Your Music</a>
  <a href="/djs">DJ Home Pages</a>
  <a href="https://static.example/contact-us">Contact Us</a>
  <a href="mailto:info@static.example">Email Us</a>
  <a href="https://unrelated.example/submit">Sponsor</a>
</body></html>
"""

# Structure B: SPA — renders no navigable <a href> in raw HTML (like a
# client-side SPA). Zero useful pages must be discovered, no fabrication.
SPA_HOME = """
<html><head><title>SPA Radio</title></head><body>
  <div id="app"></div>
  <script src="/app.js"></script>
</body></html>
"""

# Structure C: submission page discovered ONLY from a nested page, via a
# relative href that must resolve against the nested page's directory.
NESTED_HOME = """
<html><body><h1>Nested Radio</h1>
  <a href="programming/schedule">Schedule</a>
  <a href="/people/directors">Programming Director</a>
</body></html>
"""
NESTED_DIR = """
<html><body><h1>Directors</h1>
  <a href="../music/submit-music">Submit Your Music Here</a>
  <a href="mailto:md@nested.example">Music Director</a>
</body></html>
"""

# Structure D: homepage has only noisy/non-navigational links (mailto, JS,
# off-site, self-link). No navigational station page => empty useful_pages.
NOISE_HOME = """
<html><body>
  <a href="javascript:void(0)">JS</a>
  <a href="tel:+15005551234">Call</a>
  <a href="https://sponsor.example/contact">Sponsor Contact</a>
  <a href="https://noise.example/">Homepage self-link</a>
  <a href="#top">Top</a>
</body></html>
"""


def build_and_surface(station, parsed_pages, fetch_records=None):
    """build_intelligence_record -> ingest -> intelligence_payload (API JSON)."""
    enriched = build_intelligence_record(
        station, parsed_pages, fetch_records or []).to_dict()

    tmp = tempfile.TemporaryDirectory()
    try:
        service = PersistenceService(str(Path(tmp.name) / "db.sqlite"))
    except Exception:
        tmp.cleanup()
        raise
    try:
        service.ingest_intelligence([enriched], source="test")
        key = normalize_intelligence_record(enriched)[1]
        row = service.get_station(key)
        return intelligence_payload(
            station=row,
            emails=service.get_station_emails(key),
            phones=service.get_station_phones(key),
            contacts=service.get_station_contacts(key),
            submission=service.get_submission(key),
            fetches=service.get_fetches(key),
        )
    finally:
        service.close()
        tmp.cleanup()


class TestStaticStructure(unittest.TestCase):
    """Structure A: static HTML, submission + DJ directory discovered."""

    def setUp(self):
        self.station = station_dict("Static Radio", HOME_WS)
        self.parsed = pages((HOME_WS, STATIC_HOME))
        self.payload = build_and_surface(self.station, self.parsed)

    def test_exact_hrefs_preserved_verbatim(self):
        urls = {u["url"] for u in self.payload["useful_pages"]}
        self.assertIn("https://static.example/music/send", urls)
        self.assertIn("https://static.example/djs", urls)
        self.assertIn("https://static.example/contact-us", urls)
        # Relative /music/send resolved against the homepage root.
        self.assertNotIn("/music/send", urls)

    def test_labels_and_source_urls_kept(self):
        by_url = {u["url"]: u for u in self.payload["useful_pages"]}
        self.assertEqual(by_url["https://static.example/music/send"]["label"],
                         "Send Us Your Music")
        self.assertEqual(
            by_url["https://static.example/music/send"]["source_url"], HOME_WS)

    def test_send_music_action_resolves_from_useful_pages(self):
        subs = [u for u in self.payload["useful_pages"]
                if u["category"] in ("send_music", "submission_guidelines")]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["url"], "https://static.example/music/send")

    def test_off_site_and_mailto_never_surface(self):
        urls = {u["url"] for u in self.payload["useful_pages"]}
        self.assertNotIn("https://unrelated.example/submit", urls)
        self.assertFalse(any(not u["url"].startswith("http") for u in
                             self.payload["useful_pages"]))


class TestSpaStructure(unittest.TestCase):
    """Structure B: client-side SPA — no navigable anchors => no fabrication."""

    def setUp(self):
        self.station = station_dict("SPA Radio", HOME_SPA)
        self.parsed = pages((HOME_SPA, SPA_HOME))
        self.payload = build_and_surface(self.station, self.parsed)

    def test_no_useful_pages_fabricated(self):
        self.assertEqual(self.payload["useful_pages"], [])


class TestSubpageRelativeStructure(unittest.TestCase):
    """Structure C: relative href on a nested page resolves there, not root."""

    def setUp(self):
        self.station = station_dict("Nested Radio", HOME_NESTED)
        self.parsed = pages((HOME_NESTED, NESTED_HOME),
                            (HOME_NESTED + "people/directors", NESTED_DIR))
        self.payload = build_and_surface(self.station, self.parsed)

    def test_relative_resolves_against_nested_source_page(self):
        urls = {u["url"] for u in self.payload["useful_pages"]}
        # ".." from /people/ -> /music/submit-music
        self.assertIn("https://nested.example/music/submit-music", urls)
        self.assertNotIn("https://nested.example/people/music/submit-music",
                         urls)

    def test_submission_classified_from_subpage_evidence(self):
        subs = [u for u in self.payload["useful_pages"]
                if u["category"] in ("send_music", "submission_guidelines")]
        self.assertTrue(subs)
        self.assertEqual(subs[0]["url"],
                         "https://nested.example/music/submit-music")
        self.assertEqual(subs[0]["source_url"],
                         HOME_NESTED + "people/directors")


class TestNoiseOnlyStructure(unittest.TestCase):
    """Structure D: only non-navigational/noise links => honest empty pages."""

    def setUp(self):
        self.station = station_dict("Noise Radio", HOME_NOISE)
        self.parsed = pages((HOME_NOISE, NOISE_HOME))
        self.payload = build_and_surface(self.station, self.parsed)

    def test_only_noise_produces_empty_useful_pages(self):
        self.assertEqual(self.payload["useful_pages"], [])


class TestReachabilityOnlyFromRecordedFetch(unittest.TestCase):
    """Reachability flags come only from a fetch of that exact URL."""

    def test_unreachable_discovered_page_kept_and_marked(self):
        station = station_dict("Static Radio", HOME_WS)
        parsed = pages((HOME_WS, STATIC_HOME))
        fetch = [SourceFetchRecord(
            url="https://static.example/music/send", ok=False, status=404)]
        payload = build_and_surface(station, parsed, fetch)
        by_url = {u["url"]: u for u in payload["useful_pages"]}
        self.assertFalse(by_url["https://static.example/music/send"]["reachable"])
        self.assertEqual(
            by_url["https://static.example/music/send"]["status"], 404)
        self.assertEqual(by_url["https://static.example/music/send"]["url"],
                         "https://static.example/music/send")


class TestStationNoiseFiltering(unittest.TestCase):
    """Station-navigational pages only — people/instances/chrome/media excluded.

    The Useful Pages list must be station-level navigational destinations.
    Individual-people routes (DJ profiles, email forms, author pages),
    per-episode/per-item instance routes (show ids, archive ids, UUIDs),
    session/auth chrome, and media/stream assets are NOT station pages and
    must never surface. All generic — no station-specific hardcoding.
    """

    def setUp(self):
        self.station = station_dict("Noise Filter Radio", HOME_WS)
        self.parsed = pages((HOME_WS, """
        <html><body>
          <!-- genuine station-level navigational pages -->
          <a href="/music/send">Send Us Your Music</a>
          <a href="/schedule">Schedule</a>
          <a href="/contact-us">Contact Us</a>
          <!-- PEOPLE: DJ profile + per-person email form + author page -->
          <a href="/djs/albina-cabrera/">Albina Cabrera</a>
          <a href="/email.php?id=137">DJ Jesuspants</a>
          <a href="/author/erics/">By Eric Schuman</a>
          <!-- PER-ITEM / PER-EPISODE instances -->
          <a href="/playlists/shows/11198">Show #11198</a>
          <a href="/shows/episode/simplecast/3fa5ccf6-6150-4b3f-bbfa-dfa07cb1be85">Episode</a>
          <a href="/BT/Airplay_Lists/2018/2018-02-02.html">Airplay 2018-02-02</a>
          <!-- CHROME: auth / login / favicon / CDN machinery -->
          <a href="/auth.php?a=login">Login</a>
          <a href="/auth.php?a=fav_icon_clicked&type=episode&id=167921">Favicon</a>
          <a href="/cdn-cgi/l/email-protection">email protection</a>
          <!-- MEDIA / STREAM assets (not pages) -->
          <a href="/wfmu.pls">128k MP3</a>
          <a href="/audio.mp3">Audio</a>
          <!-- PLAYER INSTANCE -->
          <a href="/flashplayer.php?version=3&show=101500&archive=198344">Player</a>
        </body></html>
        """))
        self.payload = build_and_surface(self.station, self.parsed)
        self.urls = {u["url"] for u in self.payload["useful_pages"]}

    def _find(self, url):
        for u in self.payload["useful_pages"]:
            if u["url"] == url:
                return u
        return None

    def test_station_navigational_pages_kept(self):
        self.assertIn("https://static.example/music/send", self.urls)
        self.assertIn("https://static.example/schedule", self.urls)
        self.assertIn("https://static.example/contact-us", self.urls)

    def test_individual_dj_profile_excluded(self):
        # The `/djs/` collection directory is a station page, but a specific
        # person's profile under it is PEOPLE.
        self.assertNotIn("https://static.example/djs/albina-cabrera/", self.urls)

    def test_per_person_email_form_excluded(self):
        self.assertNotIn("https://static.example/email.php?id=137", self.urls)

    def test_per_person_author_page_excluded(self):
        self.assertNotIn("https://static.example/author/erics/", self.urls)

    def test_per_item_show_and_episode_instances_excluded(self):
        self.assertNotIn("https://static.example/playlists/shows/11198", self.urls)
        self.assertNotIn(
            "https://static.example/shows/episode/simplecast/"
            "3fa5ccf6-6150-4b3f-bbfa-dfa07cb1be85", self.urls)

    def test_dated_airplay_list_excluded(self):
        self.assertNotIn(
            "https://static.example/BT/Airplay_Lists/2018/2018-02-02.html",
            self.urls)

    def test_auth_and_chrome_excluded(self):
        self.assertNotIn("https://static.example/auth.php?a=login", self.urls)
        self.assertNotIn("https://static.example/auth.php?a=fav_icon_clicked"
                         "&type=episode&id=167921", self.urls)
        self.assertNotIn("https://static.example/cdn-cgi/l/email-protection",
                         self.urls)

    def test_media_and_stream_assets_excluded(self):
        self.assertNotIn("https://static.example/wfmu.pls", self.urls)
        self.assertNotIn("https://static.example/audio.mp3", self.urls)

    def test_player_instance_excluded(self):
        self.assertNotIn("https://static.example/flashplayer.php?version=3"
                         "&show=101500&archive=198344", self.urls)

    def test_exact_hrefs_kept_verbatim_no_rewriting(self):
        found = self._find("https://static.example/music/send")
        self.assertEqual(found["url"], "https://static.example/music/send")
        self.assertEqual(found["label"], "Send Us Your Music")
        self.assertEqual(found["source_url"], HOME_WS)


if __name__ == "__main__":
    unittest.main()
