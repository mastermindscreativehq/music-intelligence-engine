"""Phase 9: outreach provider abstraction + message/history records.

Covers the approved boundaries:

- ``outreach.providers``: the EmailProvider interface, status vocabulary
  (draft | opened_in_email | sent | failed), and the non-sending local
  stub — nothing may claim delivery without a provider-confirmed send.
- ``outreach.service`` over the SQLite repository: create, list, get, and
  attempt/status recording as an append-only ledger.
- ``backend.routes`` dispatch contract for /api/v1/outreach + event.
- Live stdlib webapp over HTTP for a full round-trip.

No real email is ever transmitted here.
"""

import json
import tempfile
import unittest

from backend.routes import ROUTE_TABLE, dispatch
from database.service import PersistenceService
from outreach import providers, service as osvc


def _payload(**over):
    base = {
        "recipient": {
            "contact_uid": "cu_wfmu_1",
            "identity_key": "domain:wfmu.org",
            "name": "Jessica Romoff",
            "role": "music_director",
            "organization": "WFMU",
            "email": "jessica@wfmu.org",
            "source_url": "https://wfmu.org/about",
        },
        "track": {"track_id": "sha256:abc123",
                  "original_filename": "grace.mp3", "status": "ready",
                  "size_bytes": 4106},
        "context": {"artist": {"name": "Datiam"}},
        "subject": "New music for WFMU",
        "message": "Hi Jessica, please consider our track.",
        "from": "me@example.com",
        "sharing": {"mode": "private_link",
                    "url": "https://listen.example/t/abc123"},
    }
    base.update(over)
    return base


class ProviderAbstractionTests(unittest.TestCase):
    def test_status_vocabulary(self):
        self.assertEqual([s.value for s in providers.DeliveryStatus],
                         ["draft", "opened_in_email", "sent", "failed"])
        self.assertEqual(tuple(osvc.OUTREACH_STATUSES),
                         ("draft", "opened_in_email", "sent", "failed"))

    def test_local_stub_never_claims_delivery(self):
        stub = providers.default_provider()
        self.assertEqual(stub.name, "local")
        with self.assertRaises(NotImplementedError):
            stub.send(providers.EmailEnvelope(to=["x@y.z"]))
        self.assertIsNone(stub.status_of("m1"))

    def test_mailto_provider_never_claims_send_or_attach(self):
        p = providers.MailtoProvider()
        self.assertFalse(p.can(providers.CAN_ATTACH))
        self.assertEqual(p.capabilities(), {providers.CAN_SEND,
                                            providers.CAN_ERRORS})
        res = p.send(providers.EmailEnvelope(
            to=["jessica@wfmu.org"], subject="Hi", body="Body text"))
        self.assertTrue(res.ok)
        self.assertIn("mailto:jessica@wfmu.org?", res.message_id)
        self.assertIn("subject=Hi", res.message_id)

    def test_mailto_without_recipient_fails(self):
        res = providers.MailtoProvider().send(
            providers.EmailEnvelope(to=[], subject="x"))
        self.assertFalse(res.ok)
        self.assertEqual(res.status, providers.DeliveryStatus.FAILED)


class OutreachRepositoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite",
                                                delete=False)
        self._tmp.close()
        self.svc = PersistenceService(self._tmp.name)

    def tearDown(self):
        self.svc.close()
        import os
        os.unlink(self._tmp.name)

    def test_create_lists_and_records_event(self):
        rec = osvc.create_outreach(self.svc, payload=_payload())
        self.assertTrue(rec["outreach_id"].startswith("om_"))
        self.assertEqual(rec["status"], "draft")
        self.assertEqual(rec["recipient"]["email"], "jessica@wfmu.org")
        self.assertEqual(rec["track"]["track_id"], "sha256:abc123")
        self.assertEqual(rec["attempts"], [])
        self.assertEqual(rec["links"]["self"],
                         f"/api/v1/outreach/{rec['outreach_id']}")

        rows, total = osvc.list_outreach(self.svc)
        self.assertEqual(total, 1)

        updated = osvc.record_outreach_event(
            self.svc, rec["outreach_id"], event="opened_in_email",
            meta={"channel": "mailto"})
        self.assertEqual(updated["status"], "opened_in_email")
        self.assertEqual(len(updated["attempts"]), 1)
        self.assertEqual(updated["attempts"][0]["event"],
                         "opened_in_email")
        self.assertEqual(updated["attempts"][0]["meta"]["channel"],
                         "mailto")

    def test_create_requires_email(self):
        with self.assertRaises(ValueError):
            osvc.create_outreach(self.svc, payload=_payload(
                recipient={"name": "No Email"}))

    def test_unknown_event_rejected(self):
        rec = osvc.create_outreach(self.svc, payload=_payload())
        with self.assertRaises(ValueError):
            osvc.record_outreach_event(
                self.svc, rec["outreach_id"], event="draft")
        with self.assertRaises(ValueError):
            osvc.record_outreach_event(
                self.svc, rec["outreach_id"], event="bogus")
        with self.assertRaises(LookupError):
            osvc.record_outreach_event(
                self.svc, "om_nope", event="sent")

    def test_list_status_filter(self):
        osvc.create_outreach(self.svc, payload=_payload())
        osvc.create_outreach(self.svc, payload=_payload(
            recipient={"contact_uid": "cu2", "name": "B",
                       "email": "b@example.com"}))
        _, total = osvc.list_outreach(self.svc, status="draft")
        self.assertEqual(total, 2)
        _, total = osvc.list_outreach(self.svc, status="sent")
        self.assertEqual(total, 0)

    def test_get_unknown_is_none(self):
        self.assertIsNone(osvc.get_outreach(self.svc, "om_nope"))


class OutreachDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite",
                                                delete=False)
        self._tmp.close()
        self.svc = PersistenceService(self._tmp.name)

    def tearDown(self):
        self.svc.close()
        import os
        os.unlink(self._tmp.name)

    def test_post_get_event_roundtrip(self):
        body = json.dumps(_payload()).encode("utf-8")
        status, env = dispatch(self.svc, "POST", "/api/v1/outreach", {},
                               body)
        self.assertEqual(status, 201)
        self.assertTrue(env["ok"])
        oid = env["data"]["outreach_id"]

        status2, env2 = dispatch(self.svc, "GET",
                                 f"/api/v1/outreach/{oid}", {})
        self.assertEqual(status2, 200)
        self.assertEqual(env2["data"]["status"], "draft")

        status3, env3 = dispatch(
            self.svc, "POST", f"/api/v1/outreach/{oid}/event", {},
            json.dumps({"event": "opened_in_email",
                        "meta": {"channel": "mailto"}}).encode("utf-8"))
        self.assertEqual(status3, 200)
        self.assertEqual(env3["data"]["status"], "opened_in_email")

        status4, env4 = dispatch(self.svc, "GET", "/api/v1/outreach", {})
        self.assertEqual(status4, 200)
        self.assertEqual(env4["data"]["total"], 1)

    def test_missing_email_is_400(self):
        status, env = dispatch(
            self.svc, "POST", "/api/v1/outreach", {},
            json.dumps({"recipient": {"name": "x"}}).encode("utf-8"))
        self.assertEqual(status, 400)
        self.assertFalse(env["ok"])

    def test_unknown_outreach_is_404(self):
        status, env = dispatch(self.svc, "GET",
                               "/api/v1/outreach/om_deadbeef", {})
        self.assertEqual(status, 404)
        self.assertEqual(env["error"]["code"], "outreach_not_found")

    def test_route_table_defines_outreach(self):
        verbs = {(m, template) for m, _p, template, _q in ROUTE_TABLE}
        self.assertIn(("POST", "/api/v1/outreach"), verbs)
        self.assertIn(("GET", "/api/v1/outreach"), verbs)
        self.assertIn(("POST", "/api/v1/outreach/{outreach_id}/event"),
                      verbs)
        self.assertIn(("GET", "/api/v1/outreach/{outreach_id}"), verbs)


if __name__ == "__main__":
    unittest.main()
