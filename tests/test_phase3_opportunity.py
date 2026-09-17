"""Phase 3: music-opportunity intelligence layer tests.

Covers all 19 required coverage items plus routing/dispatch contracts.
All data lives in temp directories; no real DB is touched. Tests run
in plain ``unittest`` (no external test runner required).
"""

from __future__ import annotations

import os
import tempfile
import unittest

from backend import outreach_intel as oi
from backend.routes import dispatch
from database.service import PersistenceService
from discovery.radio.contract import derive_location_status
from opportunity import scoring, service as opp_service
from outreach import service as outreach_service
from outreach.service import LocalStubProvider

import opportunity.scoring as sc

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

hexdigest = "f" * 64
track_id = f"sha256:{hexdigest}"

TRACK = {
    "track_id": track_id,
    "sha256": hexdigest,
    "original_filename": "test.mp3",
    "size_bytes": 2048,
    "content_type": "audio/mpeg",
    "status": "ready",
}


def _station(name, *, domain=None, website=None, genres=None, formats=None,
             station_type="community", city=None, state_or_region=None,
             country=None, confidence_score=0.7, status="enriched",
             contacts=None, submission=None, useful_pages=None):
    """Build a minimal ingestable intelligence record."""
    rec = {
        "name": name,
        "website": website,
        "station_type": station_type,
        "genres": genres or [],
        "formats": formats or [],
        "city": city,
        "state_or_region": state_or_region,
        "country": country,
        "confidence_score": confidence_score,
        "status": status,
        "contacts": contacts or [],
        "submission": submission,
    }
    if useful_pages is not None:
        rec["raw_metadata"] = {"useful_pages": useful_pages}
    return rec


def _contact(name, role, email=None, *, uid=None):
    return {"contact_uid": uid, "name": name, "role": role,
            "email": email, "source_url": None,
            "preferred_for_submissions": False,
            "confidence_score": None, "provenance": None}


GENRE_MATCH = _station(
    "W Genre Match",
    website="https://genre-match.org",
    genres=["rock", "indie"],
    formats=["music"],
    station_type="community",
    city="Portland", state_or_region="OR", country="US",
    confidence_score=0.9,
    contacts=[
        _contact("Alice Music", "music_director",
                 "alice@genre-match.org", uid="gm_alice"),
        _contact("Bob General", "general", "bob@genre-match.org"),
    ],
    submission={"submission_url": {"value": "https://genre-match.org/submit",
                                   "verified": True, "source_url": "https://genre-match.org/"}},
)

GENRE_MISMATCH = _station(
    "W Genre Mismatch",
    website="https://mismatch-radio.org",
    genres=["jazz"],
    formats=["news"],
    confidence_score=0.5,
    contacts=[_contact("Carol Program", "program_director",
                   None, uid="km_carol")],
)

NO_ROUTE = _station(
    "W No Route",
    website="https://noret-radio.org",
    station_type="lpfm",
    genres=["rock"],
    confidence_score=0.3,
    contacts=[],
    submission=None,
)


# ---------------------------------------------------------------------------
# Scoring unit tests (pure, no DB)
# ---------------------------------------------------------------------------

class TestScoringComponents(unittest.TestCase):
    """Items 1-6, 16-19: scoring signals, tiers, explanation, missing data."""

    def test_route_verified_submission(self):
        route = {"priority": 1, "type": "submission_url",
                 "title": "submit", "value": "https://x.com/submit"}
        comp = sc.route_component([], route)
        self.assertEqual(comp["score"], 1.0)

    def test_route_verified_submission_page(self):
        route = {"priority": 1, "type": "submission_page",
                 "title": "Submit", "value": "https://x.com/submit"}
        comp = sc.route_component([], route)
        self.assertAlmostEqual(comp["score"], 0.9)

    def test_route_discovered_not_verified(self):
        route = {"priority": 1, "type": "submission_url",
                 "title": "x", "value": "https://x.com/submit",
                 "verification_state": oi.DISCOVERED}
        comp = sc.route_component([route], None)
        self.assertEqual(comp["score"], 0.25)

    def test_route_none_recorded(self):
        comp = sc.route_component([], None)
        self.assertEqual(comp["score"], 0.1)

    def test_route_priority_hierarchy(self):
        for pri, expected in [(1, 1.0), (2, 0.75), (3, 0.5), (4, 0.3)]:
            route = {"priority": pri, "type": "other", "title": "x",
                     "value": "http://x.com"}
            comp = sc.route_component([], route)
            self.assertEqual(comp["score"], expected, f"priority {pri}")

    def test_contact_music_director_verified_email(self):
        contact = _contact("Alice", "music_director", "a@x.com")
        route = {"level": 1, "label": "Verified direct email",
                 "verification_state": oi.VERIFIED}
        comp = sc.contact_component(contact, route)
        self.assertEqual(comp["score"], 1.0)

    def test_contact_music_director_no_channel(self):
        contact = _contact("Alice", "music_director", None)
        comp = sc.contact_component(contact, None)
        self.assertEqual(comp["score"], 0.35)

    def test_contact_host_medium_relevance(self):
        contact = _contact("DJ Bob", "dj", "bob@x.com")
        route = {"level": 3, "label": "contact page", "value": "http://x.com"}
        comp = sc.contact_component(contact, route)
        self.assertEqual(comp["score"], 0.6)

    def test_contact_none(self):
        comp = sc.contact_component(None, None)
        self.assertEqual(comp["score"], 0.1)

    def test_genre_match(self):
        comp = sc.genre_component(["rock", "indie"], ["rock", "jazz"])
        self.assertGreater(comp["score"], 0.5)
        self.assertIn("rock", comp["note"])

    def test_genre_mismatch(self):
        comp = sc.genre_component(["rock"], ["jazz", "blues"])
        self.assertEqual(comp["score"], 0.0)

    def test_release_no_genre(self):
        comp = sc.genre_component(None, ["rock"])
        self.assertEqual(comp["score"], 0.4)
        self.assertIn("no genre", comp["note"].lower())

    def test_station_no_genre(self):
        comp = sc.genre_component(["rock"], None)
        self.assertEqual(comp["score"], 0.5)

    def test_format_music(self):
        comp = sc.format_component(["music", "variety"])
        self.assertEqual(comp["score"], 1.0)

    def test_format_news_only(self):
        comp = sc.format_component(["news", "talk"])
        self.assertEqual(comp["score"], 0.3)

    def test_format_unknown(self):
        comp = sc.format_component(None)
        self.assertEqual(comp["score"], 0.5)

    def test_location_available(self):
        station = _station("W", city="Portland", country="US")
        station["location_status"] = derive_location_status(
            station.get("country"), station.get("state_or_region"),
            station.get("city"), station.get("market_area"), None)
        comp = sc.location_component(station)
        self.assertEqual(comp["score"], 0.7)

    def test_location_verified(self):
        station = _station("W", city="Portland", country="US")
        station["location_status"] = derive_location_status(
            station.get("country"), station.get("state_or_region"),
            station.get("city"), station.get("market_area"), "2026-01-01T00:00:00Z")
        comp = sc.location_component(station)
        self.assertEqual(comp["score"], 0.9)

    def test_location_unavailable(self):
        station = _station("W")
        station["location_status"] = derive_location_status(
            station.get("country"), station.get("state_or_region"),
            station.get("city"), station.get("market_area"), None)
        comp = sc.location_component(station)
        self.assertEqual(comp["score"], 0.5)

    def test_release_ready(self):
        comp = sc.release_component(TRACK)
        self.assertEqual(comp["score"], 1.0)

    def test_release_quarantined(self):
        comp = sc.release_component({**TRACK, "status": "quarantined"})
        self.assertEqual(comp["score"], 0.3)

    def test_release_none(self):
        comp = sc.release_component(None)
        self.assertEqual(comp["score"], 0.3)

    def test_identity_verified_domain(self):
        comp = sc.identity_component({"domain": "real.org", "status": "enriched"})
        self.assertEqual(comp["score"], 1.0)

    def test_identity_reserved_domain(self):
        comp = sc.identity_component({"domain": "test.example"})
        self.assertEqual(comp["score"], 0.6)

    def test_identity_no_domain(self):
        comp = sc.identity_component({})
        self.assertEqual(comp["score"], 0.4)

    def test_record_full(self):
        station = _station("W", website="https://x.com", city="A",
                           country="US", genres=["rock"], formats=["music"])
        contacts = [_contact("A", "music_director", "a@x.com")]
        pages = [{"category": "send_music", "url": "https://x.com/submit"}]
        comp = sc.record_component(station, contacts, pages,
                                   {"submission_url": {"value": "http://x"}})
        self.assertGreaterEqual(comp["score"], 0.7)

    def test_record_empty(self):
        comp = sc.record_component({}, [], [], None)
        self.assertLess(comp["score"], 0.2)


class TestScoringAggregate(unittest.TestCase):
    """Tier thresholds, weighted score, penalties, explanation structure."""

    def test_tier_high(self):
        self.assertEqual(sc.tier_for(70), sc.TIER_HIGH)
        self.assertEqual(sc.tier_for(100), sc.TIER_HIGH)

    def test_tier_medium(self):
        self.assertEqual(sc.tier_for(69), sc.TIER_MEDIUM)
        self.assertEqual(sc.tier_for(40), sc.TIER_MEDIUM)

    def test_tier_low(self):
        self.assertEqual(sc.tier_for(39), sc.TIER_LOW)
        self.assertEqual(sc.tier_for(0), sc.TIER_LOW)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(sc.WEIGHTS.values()), 1.0)

    def test_score_range(self):
        station = _station("W", genres=["rock"], formats=["music"],
                           website="https://x.org",
                           city="A", country="US", confidence_score=1.0)
        contacts = [_contact("A", "music_director", "a@x.org")]
        route = {"priority": 1, "type": "submission_url", "title": "x",
                 "value": "http://x.org/submit",
                 "verification_state": oi.VERIFIED,
                 "evidence_state": oi.VERIFIED,
                 "confidence": 0.9}
        contact_route = {"level": 1, "label": "verified email",
                         "value": "mailto:a@x.org",
                         "verification_state": oi.VERIFIED}
        result = sc.score_opportunity(
            track=TRACK, station=station, contacts=contacts,
            useful_pages=[], submission={"submission_url": {"value": "http://x.org/submit"}},
            routes=[route], best_route=route,
            music_contact=contacts[0], contact_route=contact_route)
        self.assertGreaterEqual(result["score"], 70)
        self.assertEqual(result["tier"], sc.TIER_HIGH)
        self.assertIsInstance(result["reasons"], list)
        self.assertTrue(len(result["reasons"]) > 0)

    def test_already_contacted_penalty(self):
        station = _station("W", genres=[], formats=[])
        base = sc.score_opportunity(
            track=TRACK, station=station, contacts=[],
            useful_pages=[], submission=None, routes=[], best_route=None)
        modified = sc.score_opportunity(
            track=TRACK, station=station, contacts=[],
            useful_pages=[], submission=None, routes=[], best_route=None,
            already_contacted=True, contacted_at="2026-01-01T00:00:00Z",
            history=[{"outreach_id": "om_test", "status": "ready"}])
        self.assertEqual(modified["already_contacted"], True)
        self.assertGreater(modified["penalty"], 0)
        self.assertEqual(base["score"] - modified["score"], modified["penalty"])
        self.assertTrue(any(r["sign"] == "!" for r in modified["reasons"]))

    def test_reasons_contain_signs(self):
        station = _station("W")
        result = sc.score_opportunity(
            track=TRACK, station=station, contacts=[],
            useful_pages=[], submission=None, routes=[], best_route=None)
        signs = {r["sign"] for r in result["reasons"]}
        self.assertTrue(signs.issubset({"+", "-", "i"}))


# ---------------------------------------------------------------------------
# Service integration tests (temp sqlite + PersistenceService)
# ---------------------------------------------------------------------------

class _BaseDBTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        db_path = os.path.join(self._tmp, "test.db")
        self.service = PersistenceService(db_path)
        # Seed three stations
        self.service.ingest_intelligence(
            [GENRE_MATCH, GENRE_MISMATCH, NO_ROUTE], source="test")
        self.track = self.service.save_track(dict(TRACK))
        self.kx = "domain:genre-match.org"
        self.km = "domain:mismatch-radio.org"
        self.kr = "domain:noret-radio.org"

    def tearDown(self):
        self.service.close()

    def _outreach_count(self):
        return self.service.list_outreach(limit=1000, offset=0)[1]

    def _station_keys(self):
        rows, _total, _dev = self.service.list_stations(
            limit=1000, offset=0, exclude_dev=True)
        return {r["identity_key"] for r in rows}


class TestServiceCompute(_BaseDBTest):
    """Items 7-15, 17: compute_opportunities contract and behavior."""

    def test_returns_all_active_stations(self):
        result = opp_service.compute_opportunities(self.service, self.track)
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        self.assertEqual(keys, {self.kx, self.km, self.kr})
        self.assertEqual(result["total"], 3)

    def test_score_ranked_desc(self):
        result = opp_service.compute_opportunities(self.service, self.track)
        scores = [o["score"] for o in result["opportunities"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_score_ranked_asc(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, order="asc")
        scores = [o["score"] for o in result["opportunities"]]
        self.assertEqual(scores, sorted(scores))

    def test_sort_by_name(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, sort="name", order="desc")
        names = [o["station"]["name"] for o in result["opportunities"]]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_filter_genre(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, genre="rock")
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        self.assertIn(self.kx, keys)
        self.assertNotIn(self.km, keys)

    def test_filter_tier(self):
        # High tier requires strong score; only kx likely high
        result_high = opp_service.compute_opportunities(
            self.service, self.track, tier="HIGH")
        for opp in result_high["opportunities"]:
            self.assertEqual(opp["tier"], "HIGH")
        result_low = opp_service.compute_opportunities(
            self.service, self.track, tier="LOW")
        for opp in result_low["opportunities"]:
            self.assertEqual(opp["tier"], "LOW")

    def test_filter_outreach_status(self):
        # None have outreach yet; filter new should return all
        result = opp_service.compute_opportunities(
            self.service, self.track, outreach_status="new")
        self.assertEqual(result["total"], 3)

    def test_filter_has_contact(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, has_contact=True)
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        # kx and km have contacts; kr does not
        self.assertIn(self.kx, keys)
        self.assertIn(self.km, keys)
        self.assertNotIn(self.kr, keys)

    def test_filter_has_route_verified(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, has_route=True)
        # kx has a verified submission route
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        self.assertIn(self.kx, keys)

    def test_filter_station_type(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, station_type="lpfm")
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        self.assertEqual(keys, {self.kr})

    def test_filter_country(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, country="US")
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        self.assertEqual(keys, {self.kx})

    def test_pagination(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, limit=1, offset=0)
        self.assertEqual(len(result["opportunities"]), 1)
        self.assertEqual(result["total"], 3)

    def test_empty_after_overfilter(self):
        result = opp_service.compute_opportunities(
            self.service, self.track, genre="electronic")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["opportunities"], [])

    def test_reachable_for_verified_route(self):
        result = opp_service.compute_opportunities(self.service, self.track)
        kx_opp = next(o for o in result["opportunities"]
                      if o["station"]["identity_key"] == self.kx)
        self.assertTrue(kx_opp["reachable"])
        self.assertIsNotNone(kx_opp["recipient"])
        self.assertTrue(kx_opp["route_verified"])

    def test_unreachable_for_no_route_station(self):
        result = opp_service.compute_opportunities(self.service, self.track)
        kr_opp = next(o for o in result["opportunities"]
                      if o["station"]["identity_key"] == self.kr)
        self.assertFalse(kr_opp["reachable"])
        self.assertIsNone(kr_opp["recipient"])
        self.assertFalse(kr_opp["route_verified"])

    def test_existing_stations_unchanged(self):
        opp_service.compute_opportunities(self.service, self.track)
        keys = self._station_keys()
        self.assertEqual(keys, {self.kx, self.km, self.kr})

    def test_existing_tracks_unchanged(self):
        before = self.service.get_track(track_id)
        opp_service.compute_opportunities(self.service, self.track)
        after = self.service.get_track(track_id)
        self.assertEqual(before, after)

    def test_existing_outreach_unchanged(self):
        before = self._outreach_count()
        opp_service.compute_opportunities(self.service, self.track)
        self.assertEqual(self._outreach_count(), before)

    def test_calculation_creates_no_records(self):
        opp_service.compute_opportunities(self.service, self.track)
        self.assertEqual(self._outreach_count(), 0)

    def test_reasoning_structure(self):
        result = opp_service.compute_opportunities(self.service, self.track)
        for opp in result["opportunities"]:
            reasoning = opp["reasoning"]
            self.assertIn("components", reasoning)
            self.assertIn("reasons", reasoning)
            self.assertIn("penalty", reasoning)
            self.assertIsInstance(reasoning["components"], list)
            self.assertIsInstance(reasoning["reasons"], list)
            self.assertEqual(len(reasoning["components"]), 8)


class TestServiceDuplicateDetection(_BaseDBTest):
    """Existing outreach shows already_contacted and history."""

    def test_already_contacted_shows_status_and_history(self):
        payload = {
            "recipient": {
                "contact_uid": "wf_test", "identity_key": self.kx,
                "name": "Music", "role": "submission",
                "email": "", "organization": "W Genre Match",
                "submission_url": "https://genre-match.example.com/submit",
                "outreach_class": "webform"},
            "track": {"track_id": track_id},
            "subject": "test", "message": "test",
        }
        outreach_service.create_outreach(
            self.service, payload=payload, provider=LocalStubProvider())
        self.assertEqual(self._outreach_count(), 1)
        result = opp_service.compute_opportunities(self.service, self.track)
        kx_opp = next(o for o in result["opportunities"]
                      if o["station"]["identity_key"] == self.kx)
        self.assertTrue(kx_opp["already_contacted"])
        self.assertEqual(kx_opp["outreach_status"], "contacted")
        self.assertEqual(len(kx_opp["history"]), 1)
        self.assertGreater(kx_opp["reasoning"]["penalty"], 0)


class TestServiceExistingStationsUnchanged(_BaseDBTest):
    """Items 17: station with no contact still surfaces in results."""

    def test_no_contact_station_still_present(self):
        result = opp_service.compute_opportunities(self.service, self.track)
        kr_opp = next(o for o in result["opportunities"]
                      if o["station"]["identity_key"] == self.kr)
        self.assertIsNone(kr_opp["recommended_contact"])
        self.assertFalse(kr_opp["reachable"])


# ---------------------------------------------------------------------------
# Dispatch integration (backend.routes.dispatch)
# ---------------------------------------------------------------------------

class TestDispatchOpportunities(_BaseDBTest):
    def test_missing_track_id_returns_400(self):
        status, body = dispatch(self.service, "GET",
                                "/api/v1/opportunities", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_unknown_track_returns_404(self):
        status, body = dispatch(
            self.service, "GET",
            "/api/v1/opportunities", {"track_id": ["sha256:" + "0"*64]})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "track_not_found")

    def test_valid_track_returns_200(self):
        status, body = dispatch(
            self.service, "GET",
            "/api/v1/opportunities",
            {"track_id": [track_id]})
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        self.assertIn("opportunities", body["data"])
        self.assertIn("track", body["data"])

    def test_filter_params_pass_through(self):
        status, body = dispatch(
            self.service, "GET",
            "/api/v1/opportunities",
            {"track_id": [track_id], "genre": ["rock"], "has_contact": ["true"]})
        self.assertEqual(status, 200)
        for opp in body["data"]["opportunities"]:
            self.assertIn("rock", [g.strip() for g in (opp["station"].get("genres") or [])])
            self.assertIsNotNone(opp["recommended_contact"])

    def test_invalid_tier_returns_400(self):
        status, body = dispatch(
            self.service, "GET",
            "/api/v1/opportunities",
            {"track_id": [track_id], "tier": ["INVALID"]})
        self.assertEqual(status, 400)
        self.assertIn("tier", body["error"]["message"])

    def test_invalid_outreach_status_returns_400(self):
        status, body = dispatch(
            self.service, "GET",
            "/api/v1/opportunities",
            {"track_id": [track_id], "outreach_status": ["maybe"]})
        self.assertEqual(status, 400)
        self.assertIn("outreach_status", body["error"]["message"])

    def test_invalid_sort_returns_400(self):
        status, body = dispatch(
            self.service, "GET",
            "/api/v1/opportunities",
            {"track_id": [track_id], "sort": ["price"]})
        self.assertEqual(status, 400)

    def test_post_not_allowed(self):
        status, body = dispatch(
            self.service, "POST", "/api/v1/opportunities", {})
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
