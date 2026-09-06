"""Phase 11: outreach-intelligence derivation — evidence model, route
hierarchy, contact-route vocabulary, useful-page semantics, primary
recommendation, and honest station levels.

Pure unit coverage: no network, no storage. The derivation functions are
read-path only and must stay deterministic and evidence-faithful; the
contract wiring asserts the new surface actually reaches the API payloads.
"""

import unittest

from backend import outreach_intel as oi
from backend.contracts import contacts_payload, intelligence_payload


def _page(url, category="other", reachable=None, label="link",
          source_url="https://station.radio/"):
    return {
        "url": url, "label": label, "category": category,
        "source_url": source_url, "method": "link",
        "discovered_at": "2026-01-01T00:00:00Z",
        "rechecked_at": "", "reachable": reachable, "status": None,
        "provenance": [],
    }


def _contact(cuid, name, role, email=None, phone=None, source_url=None):
    return {
        "contact_uid": cuid, "name": name, "role": role,
        "email": email, "phone": phone,
        "source_url": source_url or "https://station.radio/staff",
        "verified_at": "2026-01-01T00:00:00Z", "preferred_for_submissions": False,
        "provenance": [],
    }


def _submission(**kw):
    base = {
        "submission_url": {"value": "https://station.radio/send",
                           "verified": True,
                           "source_url": "https://station.radio/about",
                           "discovered_at": "2026-01-01T00:00:00Z"},
        "submission_email": "md@station.radio",
        "instructions": "Send formatted music",
        "methods": {"methods": ["webform"], "kind": "inference",
                    "reasons": ["submission page observed"]},
        "verified": True,
    }
    base.update(kw)
    return base


def _station(**kw):
    base = {
        "identity_key": "domain:station.radio", "name": "Station FM",
        "organization_type": "radio_station", "domain": "station.radio",
        "website": "https://station.radio", "status": "enriched",
        "raw_metadata": {"useful_pages": []}, "source_urls": [],
    }
    base.update(kw)
    return base


class EvidenceStateVocabularyTests(unittest.TestCase):
    def test_page_reachable_is_VERIFIED(self):
        self.assertEqual(oi.page_evidence_state(_page("x", reachable=True)),
                         oi.VERIFIED)

    def test_page_unreachable_is_UNREACHABLE(self):
        self.assertEqual(oi.page_evidence_state(_page("x", reachable=False)),
                         oi.UNREACHABLE)

    def test_page_unchecked_is_DISCOVERED(self):
        self.assertEqual(oi.page_evidence_state(_page("x", reachable=None)),
                         oi.DISCOVERED)

    def test_contact_email_is_VERIFIED(self):
        self.assertEqual(oi.contact_evidence_state(_contact(
            "c", "A", "music_director", email="a@x.radio")), oi.VERIFIED)

    def test_contact_phone_is_DISCOVERED_not_claimed_verified(self):
        self.assertEqual(oi.contact_evidence_state(_contact(
            "c", "A", "music_director", phone="+1 555 0100")), oi.DISCOVERED)

    def test_contact_without_channel_is_DISCOVERED(self):
        self.assertEqual(oi.contact_evidence_state(_contact("c", "A", "dj")),
                         oi.DISCOVERED)


class ReservedTldHonestyTests(unittest.TestCase):
    def test_example_email_is_not_verified_evidence(self):
        contact = _contact("x", "Dion", "music_director",
                           email="dion@kzow.example")
        self.assertEqual(oi.contact_evidence_state(contact), oi.DISCOVERED)

    def test_example_submission_email_route_is_not_actionable(self):
        sub = _submission(submission_url=None,
                          submission_email="md@kzow.example")
        routes = oi.build_outreach_routes([], sub, [])
        email_routes = [r for r in routes
                        if r["type"] == "submission_email"]
        self.assertTrue(email_routes)
        for route in email_routes:
            self.assertNotEqual(route["verification_state"], oi.ACTIONABLE)

    def test_example_domain_station_is_not_claimed_verified(self):
        level, _reason = oi.station_intelligence_level(
            _station(domain="wxyz.example", website=None), [], None, [])
        self.assertEqual(level, "RAW")


class ContactRouteVocabularyTests(unittest.TestCase):
    CASES = {
        "music_director": "MUSIC_DIRECTOR",
        "program_director": "PROGRAM_DIRECTOR",
        "music_programmer": "PROGRAMMING_CONTACT",
        "music_scheduler": "PROGRAMMING_CONTACT",
        "music_submission": "DIRECT_MUSIC_CONTACT",
        "host": "SHOW_HOST",
        "dj": "DJ",
        "general": "GENERAL_STATION_CONTACT",
        "unknown": "UNKNOWN_ROLE",
        "": "UNKNOWN_ROLE",
        "mystery_role": "UNKNOWN_ROLE",
    }

    def test_route_classes(self):
        for role, expected in self.CASES.items():
            self.assertEqual(oi.contact_route_class(role), expected)


class RouteHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.station = _station()
        self.send_page = _page("https://station.radio/send", "send_music",
                               reachable=True, label="Send Us Your Music")
        self.contact_page = _page("https://station.radio/contact", "contact",
                                  reachable=True, label="Contact")
        self.dj_page = _page("https://station.radio/djs", "dj_directory",
                             reachable=True, label="DJs")
        self.director = _contact("cu_d", "Dion", "music_director",
                                 email="dion@station.radio")
        self.dj = _contact("cu_j", "Jay", "dj")

    def test_submission_url_route_is_tier_one(self):
        routes = oi.build_outreach_routes([], _submission(), [])
        self.assertEqual(routes[0]["priority"], oi.P1_DIRECT_SUBMISSION)
        self.assertEqual(routes[0]["class"], "DIRECT_MUSIC_SUBMISSION")
        self.assertEqual(routes[0]["verification_state"], oi.ACTIONABLE)
        self.assertEqual(routes[0]["value"], "https://station.radio/send")

    def test_send_page_still_tier_one(self):
        routes = oi.build_outreach_routes([self.send_page], None, [])
        self.assertEqual(routes[0]["priority"], oi.P1_DIRECT_SUBMISSION)
        self.assertEqual(routes[0]["verification_state"], oi.ACTIONABLE)

    def test_music_director_email_is_tier_two(self):
        routes = oi.build_outreach_routes([], None, [self.director])
        self.assertEqual(routes[0]["priority"], oi.P2_MUSIC_CONTACT)
        self.assertEqual(routes[0]["class"], "MUSIC_DIRECTOR")
        self.assertEqual(routes[0]["verification_state"], oi.ACTIONABLE)

    def test_contact_page_is_tier_three(self):
        routes = oi.build_outreach_routes([self.contact_page], None, [])
        self.assertEqual(routes[0]["priority"], oi.P3_GENERAL_CONTACT)
        self.assertEqual(routes[0]["class"], "GENERAL_CONTACT")

    def test_directory_page_is_tier_four(self):
        routes = oi.build_outreach_routes([self.dj_page], None, [])
        self.assertEqual(routes[0]["priority"], oi.P4_DIRECTORY)
        self.assertEqual(routes[0]["class"], "DJ_PROGRAM_DIRECTORY")

    def test_full_hierarchy_orders_p1_p2_p3_p4(self):
        routes = oi.build_outreach_routes(
            [self.contact_page, self.dj_page, self.send_page],
            _submission(), [self.director, self.dj])
        priorities = [r["priority"] for r in routes]
        self.assertEqual(priorities, sorted(priorities))
        self.assertEqual(priorities[0], oi.P1_DIRECT_SUBMISSION)
        self.assertTrue(oi.P2_MUSIC_CONTACT in priorities)
        self.assertTrue(oi.P3_GENERAL_CONTACT in priorities)
        self.assertTrue(oi.P4_DIRECTORY in priorities)

    def test_unreachable_send_page_is_not_actionable(self):
        routes = oi.build_outreach_routes(
            [_page("https://station.radio/send", "send_music",
                   reachable=False)], None, [])
        self.assertNotEqual(routes[0]["verification_state"], oi.ACTIONABLE)

    def test_dj_without_channel_is_named_reference_not_actionable(self):
        routes = oi.build_outreach_routes([], None, [self.dj])
        self.assertFalse(routes[0].get("value"))
        self.assertNotEqual(routes[0]["verification_state"], oi.ACTIONABLE)

    def test_best_route_is_top_verified(self):
        routes = oi.build_outreach_routes(
            [self.contact_page, self.send_page], _submission(), [self.director])
        best = oi.best_outreach_route(routes)
        self.assertIsNotNone(best)
        self.assertEqual(best["priority"], oi.P1_DIRECT_SUBMISSION)
        self.assertEqual(best["value"], "https://station.radio/send")

    def test_no_verified_route_means_no_best(self):
        routes = oi.build_outreach_routes([], None, [self.dj])  # no channel
        self.assertIsNone(oi.best_outreach_route(routes))


class UsefulPageSemanticsTests(unittest.TestCase):
    def test_every_category_carries_why_next_step_kind(self):
        for category in ("send_music", "submission_guidelines", "contact",
                         "dj_directory", "programming", "about", "other"):
            page = _page(f"https://x.radio/{category}", category)
            semantics = oi.PAGE_ROUTE_SEMANTICS[category]
            self.assertTrue(semantics["why"])
            self.assertTrue(semantics["next_step"])
            self.assertTrue(semantics["kind"])
            self.assertIn(semantics["actionability"],
                          ("High", "Medium", "Low"))
            self.assertEqual(page["category"], category)


class PrimaryRecommendationTests(unittest.TestCase):
    def test_recommendation_with_verified_route(self):
        rec = oi.primary_recommendation(
            _station(),
            [_page("https://station.radio/send", "send_music",
                   reachable=True)], _submission(), [])
        self.assertIsNotNone(rec["route"])
        self.assertIn("verified", rec["why"].lower())
        self.assertEqual(rec["confidence"], "High")
        self.assertTrue(rec["evidence"])
        self.assertTrue(rec["fallback"])
        self.assertTrue(rec["route_count"] >= 1)

    def test_recommendation_without_evidence_is_honest(self):
        rec = oi.primary_recommendation(_station(), [], None, [])
        self.assertIsNone(rec["route"])
        self.assertEqual(rec["confidence"], oi.UNKNOWN)
        self.assertIn("verified direct music submission route", rec["unknown"])
        self.assertEqual(rec["route_count"], 0)

    def test_recommendation_unknowns_document_only_missing_things(self):
        rec = oi.primary_recommendation(
            _station(), [_page("https://station.radio/send", "send_music",
                               reachable=True)], _submission(),
            [_contact("cu_d", "Dion", "music_director",
                      email="dion@station.radio")])
        self.assertNotIn("verified direct music submission route",
                         rec["unknown"])
        self.assertNotIn("verified named music decision-maker",
                         rec["unknown"])


class StationLevelTests(unittest.TestCase):
    def test_actionable_when_direct_route_verified(self):
        level, reason = oi.station_intelligence_level(
            _station(),
            [_page("https://station.radio/send", "send_music",
                   reachable=True)], _submission(), [])
        self.assertEqual(level, "ACTIONABLE")
        self.assertTrue(reason)

    def test_limited_when_only_general_route_verified(self):
        level, _reason = oi.station_intelligence_level(
            _station(),
            [_page("https://station.radio/contact", "contact",
                   reachable=True)], None, [])
        self.assertEqual(level, "LIMITED")

    def test_insufficient_evidence_when_investigated_but_no_route(self):
        level, _reason = oi.station_intelligence_level(
            _station(),
            [_page("https://station.radio/about", "about", reachable=True)],
            None, [])
        self.assertEqual(level, "INSUFFICIENT_EVIDENCE")

    def test_verified_when_identity_only(self):
        level, _reason = oi.station_intelligence_level(_station(), [], None, [])
        self.assertEqual(level, "VERIFIED")

    def test_raw_when_literally_nothing(self):
        level, _reason = oi.station_intelligence_level(
            _station(domain=None, website=None, identity_key="namegeo:x"), [],
            None, [])
        self.assertEqual(level, "RAW")


class ContractWiringTests(unittest.TestCase):
    def test_intelligence_payload_carries_level_routes_recommendation(self):
        station = _station(raw_metadata={"useful_pages": [
            _page("https://station.radio/send", "send_music",
                  reachable=True)],
        })
        contacts = [self._director()]
        payload = intelligence_payload(station, [
            {"value": "dion@station.radio", "source_url": "x",
             "method": "staff"}], [], contacts, _submission())
        self.assertEqual(payload["intelligence_level"], "ACTIONABLE")
        self.assertTrue(payload["intelligence_level_reason"])
        self.assertTrue(payload["outreach_routes"])
        self.assertTrue(payload["outreach_recommendation"]["route"])
        self.assertEqual(payload["outreach_recommendation"]["route_count"],
                         len(payload["outreach_routes"]))

    def test_useful_page_view_has_semantics(self):
        station = _station(raw_metadata={"useful_pages": [
            _page("https://station.radio/send", "send_music",
                  reachable=True)]})
        payload = intelligence_payload(station, [], [], [], None)
        page = payload["useful_pages"][0]
        self.assertTrue(page["why"])
        self.assertTrue(page["next_step"])
        self.assertIn("Submission route", page["kind"])
        self.assertEqual(page["route_class"], "DIRECT_MUSIC_SUBMISSION")

    def test_contact_view_has_route_class(self):
        payload = contacts_payload(_station(), [self._director()], None)
        view = payload["contacts"][0]
        self.assertEqual(view["route_class"], "MUSIC_DIRECTOR")
        self.assertEqual(payload["intelligence_level"], "ACTIONABLE")
        self.assertTrue(payload["outreach_recommendation"]["route"])

    def test_contacts_payload_surfaces_recommendation_section(self):
        payload = contacts_payload(_station(), [self._director()], _submission())
        self.assertEqual(payload["outreach_recommendation"]["confidence"],
                         "High")
        self.assertTrue(payload["outreach_recommendation"]["unknown"])
        self.assertTrue(payload["outreach_routes"])

    def test_enriched_status_is_not_claimed_as_level(self):
        station = _station(status="enriched", raw_metadata={"useful_pages": []})
        payload = intelligence_payload(station, [], [], [], None)
        self.assertIn(payload["intelligence_level"],
                      ("RAW", "VERIFIED", "LIMITED", "ACTIONABLE",
                       "INSUFFICIENT_EVIDENCE"))

    @staticmethod
    def _director():
        return _contact("cu_d", "Dion", "music_director",
                        email="dion@station.radio")


if __name__ == "__main__":
    unittest.main()