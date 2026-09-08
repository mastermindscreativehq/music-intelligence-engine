"""Phase 11-b: per-contact reachability — relevance, available/best outreach
routes, and the contact-vs-route separation the console must present.

A named decision-maker with no verified email is NOT "not reachable" while
an official station route exists. Coverage is pure read-path: no network,
no storage.
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


def _contact(cuid, name, role, email=None, phone=None, source_url=None,
             preferred=False):
    return {
        "contact_uid": cuid, "name": name, "role": role,
        "email": email, "phone": phone,
        "source_url": source_url or "https://station.radio/staff",
        "verified_at": "2026-01-01T00:00:00Z", "preferred_for_submissions": preferred,
        "provenance": [],
    }


def _station(**kw):
    base = {
        "identity_key": "domain:station.radio", "name": "Station FM",
        "organization_type": "radio_station", "domain": "station.radio",
        "website": "https://station.radio", "status": "enriched",
        "raw_metadata": {"useful_pages": []}, "source_urls": [],
    }
    base.update(kw)
    return base


def _send_page():
    return _page("https://station.radio/send", "send_music", reachable=True,
                 label="Send Us Your Music", source_url="https://station.radio/")


def _contact_page():
    return _page("https://station.radio/contact", "contact", reachable=True,
                 label="Contact", source_url="https://station.radio/")


def _programming_page():
    return _page("https://station.radio/programming", "programming",
                 reachable=True, label="Programming", source_url="https://station.radio/")


def _dj_page():
    return _page("https://station.radio/djs", "dj_directory", reachable=True,
                 label="DJs", source_url="https://station.radio/")


class ContactRelevanceTests(unittest.TestCase):
    def test_music_director_is_high(self):
        rel = oi.contact_relevance(_contact("c", "A", "music_director"))
        self.assertEqual(rel["label"], "High")
        self.assertGreaterEqual(rel["score"], 0.75)
        self.assertTrue(rel["reason"])

    def test_preferred_flag_lifts_relevance(self):
        rel = oi.contact_relevance(_contact("c", None, "music_director",
                                            preferred=True))
        self.assertEqual(rel["label"], "High")

    def test_dj_is_medium(self):
        rel = oi.contact_relevance(_contact("c", "Jay", "dj"))
        self.assertEqual(rel["label"], "Medium")

    def test_unknown_role_is_low(self):
        rel = oi.contact_relevance(_contact("c", None, "unknown"))
        self.assertEqual(rel["label"], "Low")


class ContactOwnRouteTests(unittest.TestCase):
    def test_verified_email_is_own_route(self):
        routes = oi.contact_outreach_routes(
            _contact("c", "A", "music_director", email="md@station.radio"), [])
        self.assertEqual(routes[0]["channel"], "own")
        self.assertEqual(routes[0]["kind"], "email")
        self.assertEqual(routes[0]["level"], 1)
        self.assertEqual(routes[0]["verification_state"], oi.VERIFIED)
        self.assertEqual(routes[0]["value"], "mailto:md@station.radio")

    def test_reserved_tld_email_is_not_own_verified_route(self):
        routes = oi.contact_outreach_routes(
            _contact("c", "A", "music_director", email="md@station.example"), [])
        self.assertFalse(any(r["channel"] == "own" and r["kind"] == "email"
                             for r in routes))

    def test_phone_is_channel_evidence_not_sendable(self):
        routes = oi.contact_outreach_routes(
            _contact("c", "A", "music_director", phone="+1 555 0100"), [])
        phone = [r for r in routes if r["kind"] == "phone"]
        self.assertEqual(len(phone), 1)
        self.assertIsNone(phone[0]["value"])  # not app-sendable
        self.assertIsNone(oi.best_contact_outreach_route(
            _contact("c", "A", "music_director", phone="+1 555 0100"), []))


class ContactFallbackRouteTests(unittest.TestCase):
    STATION = _station(raw_metadata={
        "useful_pages": [_send_page(), _contact_page()]})

    def test_no_email_director_reaches_station_submission_route(self):
        contact = _contact("c", "Jessica", "music_director")
        station_routes = oi.build_outreach_routes(
            self.STATION["raw_metadata"]["useful_pages"], None, [contact])
        routes = oi.contact_outreach_routes(contact, station_routes)
        self.assertTrue(routes)
        self.assertEqual(routes[0]["channel"], "station")
        self.assertEqual(routes[0]["kind"], "webform")
        self.assertEqual(routes[0]["level"], 2)
        best = oi.best_contact_outreach_route(contact, station_routes)
        self.assertEqual(best["value"], "https://station.radio/send")
        self.assertEqual(best["verification_state"], oi.ACTIONABLE)

    def test_hierarchy_order_submission_then_contact(self):
        contact = _contact("c", "Ken", "program_director")
        station_routes = oi.build_outreach_routes(
            [_send_page(), _contact_page(), _dj_page(), _programming_page()],
            None, [contact])
        routes = oi.contact_outreach_routes(contact, station_routes)
        levels = [r["level"] for r in routes]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(routes[0]["level"], 2)     # submission route
        self.assertEqual(routes[1]["level"], 3)     # contact page
        self.assertIn(4, levels)                    # programming
        self.assertIn(5, levels)                    # dj directory

    def test_own_email_deduplicates_station_submission_email(self):
        contact = _contact("c", None, "music_director", email="md@station.radio")
        station_routes = oi.build_outreach_routes(
            [_send_page(), _contact_page()],
            {"submission_url": None,
             "submission_email": "md@station.radio"}, [contact])
        routes = oi.contact_outreach_routes(contact, station_routes)
        emails = [r["value"] for r in routes if r["kind"] == "email"]
        self.assertEqual(emails.count("mailto:md@station.radio"), 1)
        # own verified email outranks every station route
        self.assertEqual(routes[0]["channel"], "own")

    def test_no_route_at_all_is_honest(self):
        contact = _contact("c", "Ken", "program_director")
        routes = oi.contact_outreach_routes(contact, [])
        self.assertEqual(routes, [])
        self.assertIsNone(oi.best_contact_outreach_route(contact, []))


class ContractAnnotationTests(unittest.TestCase):
    def test_wfmu_like_named_contact_is_actionable(self):
        station = _station(raw_metadata={
            "useful_pages": [_send_page(), _contact_page()]})
        contacts = [_contact("c1", "Jessica Romoff", "music_director"),
                    _contact("c2", "Ken Freedman", "program_director")]
        payload = contacts_payload(station, contacts, None)
        for view in payload["contacts"]:
            self.assertEqual(view["relevance"]["label"], "High")
            self.assertIsNotNone(view["best_outreach_route"])
            self.assertTrue(view["can_add_to_campaign"])
            self.assertGreaterEqual(len(view["outreach_routes"]), 2)

    def test_kexp_like_email_director_prefers_own_email(self):
        station = _station(raw_metadata={
            "useful_pages": [_send_page(), _contact_page()]})
        director = _contact("c3", None, "music_director",
                            email="md@station.radio", preferred=True)
        payload = contacts_payload(station, [director], None)
        view = payload["contacts"][0]
        self.assertEqual(view["best_outreach_route"]["channel"], "own")
        self.assertEqual(view["best_outreach_route"]["value"],
                         "mailto:md@station.radio")
        self.assertTrue(view["can_add_to_campaign"])

    def test_route_less_contact_marked_not_actionable(self):
        station = _station(raw_metadata={"useful_pages": []})
        contact = _contact("c4", "Ken", "program_director")
        payload = contacts_payload(station, [contact], None)
        view = payload["contacts"][0]
        self.assertIsNone(view["best_outreach_route"])
        self.assertFalse(view["can_add_to_campaign"])
        self.assertEqual(view["outreach_routes"], [])

    def test_phone_only_contact_is_not_campaign_actionable(self):
        station = _station(raw_metadata={"useful_pages": []})
        contact = _contact("c5", None, "unknown", phone="+1 555 0100")
        payload = contacts_payload(station, [contact], None)
        view = payload["contacts"][0]
        self.assertFalse(view["can_add_to_campaign"])
        self.assertFalse(any(r.get("kind") == "email" or r.get("value")
                             for r in view["outreach_routes"]
                             if r["kind"] != "phone"))

    def test_intelligence_payload_annotates_contacts_too(self):
        station = _station(raw_metadata={
            "useful_pages": [_send_page()]})
        contact = _contact("c6", "Jessica Romoff", "music_director")
        payload = intelligence_payload(
            station, [], [], [contact], None)
        view = payload["contacts"][0]
        self.assertIn("relevance", view)
        self.assertIn("outreach_routes", view)
        self.assertIn("best_outreach_route", view)
        self.assertTrue(view["can_add_to_campaign"])


if __name__ == "__main__":
    unittest.main()