"""DJ candidate qualification: the strict gate a webpage must clear to become
a DJ.

User-mandated rule: DISCOVERED → VERIFIED → RELEVANT → ACTIONABLE. A webpage
being found does NOT make a qualified DJ. Event/ticketing/playlist/listing
pages and pages that merely mention DJs must never enter the DJ table; only
candidates with positive DJ evidence (personal site, platform profile, etc.)
qualify. No network and no real database are used here.
"""

from __future__ import annotations

import unittest

from discovery.djs.qualify import (
    VERDICT_NEEDS_REVIEW,
    VERDICT_QUALIFIED,
    VERDICT_REJECTED,
    classify_candidate,
)


class TestClassifyCandidate(unittest.TestCase):
    def test_eventbrite_page_is_rejected(self):
        q = classify_candidate(
            url="https://www.eventbrite.com/e/afrobeats-night-tickets-123",
            title="AFROBEATS | AMAPIANO | DANCEHALL | Eventbrite")
        self.assertEqual(q.verdict, VERDICT_REJECTED)
        self.assertIn("event", q.reason.lower())

    def test_spotify_playlist_is_rejected(self):
        q = classify_candidate(
            url="https://open.spotify.com/playlist/37i9dQZF1DX8Uebhn9wzrS",
            title="African Heat | Spotify Playlist")
        self.assertEqual(q.verdict, VERDICT_REJECTED)

    def test_ticket_master_page_is_rejected(self):
        q = classify_candidate(
            url="https://www.ticketmaster.com/event/12345",
            title="Afrobeats Festival Tickets")
        self.assertEqual(q.verdict, VERDICT_REJECTED)
        self.assertEqual(q.kind, "non_dj")

    def test_venue_listing_page_with_dj_name_is_rejected(self):
        """An event/listing page is NOT DJ evidence even when it names a DJ."""
        q = classify_candidate(
            url="https://thespot.example/events/friday-gigs",
            title="Friday Gigs | Lineup: DJ Zulu, DJ Marta — Tickets")
        self.assertEqual(q.verdict, VERDICT_REJECTED)

    def test_merely_mentioning_djs_needs_review(self):
        q = classify_candidate(
            url="https://afrobeats.example/",
            title="Afrobeats To The World",
            snippet="Featuring DJs from Lagos to London.")
        self.assertEqual(q.verdict, VERDICT_NEEDS_REVIEW)

    def test_personal_dj_site_is_qualified(self):
        q = classify_candidate(
            url="https://djamara.example/", title="DJ Amara",
            snippet="Bookings, mixes and tour dates.")
        self.assertEqual(q.verdict, VERDICT_QUALIFIED)
        self.assertEqual(q.kind, "personal_site")

    def test_platform_profile_with_dj_signal_is_qualified(self):
        q = classify_candidate(
            url="https://soundcloud.com/dj-zulu", title="DJ Zulu",
            snippet="DJ mixes from Nairobi.")
        self.assertEqual(q.verdict, VERDICT_QUALIFIED)
        self.assertEqual(q.kind, "platform_profile")

    def test_platform_profile_without_dj_signal_needs_review(self):
        """A bare artist profile could be a musician — not deemed a DJ."""
        q = classify_candidate(
            url="https://soundcloud.com/afrobeats-boy",
            title="Afrobeats Boy", snippet="Original songs.")
        self.assertEqual(q.verdict, VERDICT_NEEDS_REVIEW)

    def test_page_title_can_upgrade_to_qualified(self):
        q = classify_candidate(
            url="https://marta.example/", title="Marta's corner",
            snippet="Music blog.", page_title="DJ Marta | Media Kit")
        self.assertEqual(q.verdict, VERDICT_QUALIFIED)
        self.assertEqual(q.kind, "personal_site")

    def test_page_title_can_downgrade_to_rejected(self):
        q = classify_candidate(
            url="https://afrobeats-weekly.example/",
            title="Afrobeats Weekly",
            page_title="Afrobeats Weekly — Gig Listings & Tickets")
        self.assertEqual(q.verdict, VERDICT_REJECTED)

    def test_dj_titled_personal_page_beats_text_markers(self):
        """Positive DJ evidence outranks incidental words like 'tickets'."""
        q = classify_candidate(
            url="https://djamara.example/", title="DJ Amara",
            snippet="DJ Amara — book tickets for upcoming club nights.")
        self.assertEqual(q.verdict, VERDICT_QUALIFIED)

    def test_qualified_evidence_is_recorded(self):
        q = classify_candidate(
            url="https://djamara.example/", title="DJ Amara")
        data = q.to_dict()
        self.assertEqual(data["verdict"], VERDICT_QUALIFIED)
        self.assertEqual(data["evidence_url"], "https://djamara.example/")
        self.assertIn("evidence", data["reason"].lower())
        self.assertTrue(data["evaluated_at"])

    # -- deterministic non-DJ signatures added for the data cleanup ------------

    def test_resident_advisor_event_is_rejected(self):
        q = classify_candidate(
            url="https://ra.co/events/2398661", title="DJ Holger | RA Guide")
        self.assertEqual(q.verdict, VERDICT_REJECTED)
        self.assertEqual(q.kind, "non_dj")

    def test_yelp_search_listing_is_rejected(self):
        q = classify_candidate(
            url="https://www.yelp.com/search?find_desc=djs",
            title="Best Djs Near Me | Yelp")
        self.assertEqual(q.verdict, VERDICT_REJECTED)

    def test_facebook_group_discussion_is_rejected(self):
        q = classify_candidate(
            url="https://www.facebook.com/groups/djtalk/posts/123",
            title="DJ Facebook group post")
        self.assertEqual(q.verdict, VERDICT_REJECTED)

    def test_facebook_profile_page_can_still_qualify(self):
        """The forum marker only fires on group/post/video pages."""
        q = classify_candidate(
            url="https://www.facebook.com/djkenz/",
            title="DJ Kenz | NY & NJ DJ")
        self.assertEqual(q.verdict, VERDICT_QUALIFIED)

    def test_editorial_blog_soft_rejects_without_dj_evidence(self):
        q = classify_candidate(
            url="https://blog.afrobeats-news.com/nyc-2026/",
            title="NYC Afrobeats Scene Grows")
        self.assertEqual(q.verdict, VERDICT_REJECTED)
        self.assertEqual(q.kind, "non_dj")
        self.assertIn("blog", q.reason.lower())

    def test_editorial_blog_with_dj_evidence_still_qualifies(self):
        """Positive DJ evidence outranks the soft editorial marker."""
        q = classify_candidate(
            url="https://blog.afrobeats-news.com/nyc-2026/",
            title="DJ Kenz Books NYC Residency", page_title="DJ Kenz")
        self.assertEqual(q.verdict, VERDICT_QUALIFIED)


if __name__ == "__main__":
    unittest.main()