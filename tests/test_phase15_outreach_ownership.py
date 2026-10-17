"""Phase B: per-user ownership of outreach records.

Pins the change that closes the cross-user privacy gap: ``outreach_messages``
gained an ``owner_user_id`` column (SQLite schema v15 / Postgres 0014), the
authenticated creator is stamped on ``create_outreach``, and reads/sends are
scoped so one operator never sees or acts on another operator's records.

Legacy/anonymous rows (owner NULL) stay visible to everyone, preserving the
historical shared single-operator behavior. ``owner_user_id`` is internal and
is never exposed in the public outreach projection.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import outreach.service as outreach_service
from auth.service import AuthService
from backend.routes import ROUTE_TABLE, dispatch
from database.service import PersistenceService
from database.schema import SCHEMA_VERSION, MIGRATIONS


def _payload(email="dj@example.com", **over):
    base = {
        "recipient": {"email": email},
        "subject": "New music",
        "message": "Please consider.",
    }
    base.update(over)
    return base


class StorageOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = PersistenceService(os.path.join(self._tmp.name, "db.sqlite"))

    def tearDown(self):
        self.svc.close()
        self._tmp.cleanup()

    def _record(self, outreach_id, owner):
        return {
            "outreach_id": outreach_id, "contact_uid": "cu", "email": "a@b.c",
            "outreach_class": "email", "status": "ready", "provider": "local",
            "owner_user_id": owner,
        }

    def test_owner_round_trips_and_is_hidden_from_other_users(self):
        self.svc.save_outreach(self._record("om_alice", "user-alice"))
        own = self.svc.get_outreach("om_alice", owner_user_id="user-alice")
        self.assertIsNotNone(own)
        self.assertEqual(own["owner_user_id"], "user-alice")
        # A different user must not see another user's record.
        self.assertIsNone(
            self.svc.get_outreach("om_alice", owner_user_id="user-bob"))

    def test_legacy_null_owner_record_visible_to_everyone(self):
        self.svc.save_outreach(self._record("om_legacy", None))
        for who in ("user-alice", "user-bob", None):
            self.assertIsNotNone(
                self.svc.get_outreach("om_legacy", owner_user_id=who))

    def test_list_scoped_shows_own_plus_legacy_only(self):
        self.svc.save_outreach(self._record("om_a", "user-alice"))
        self.svc.save_outreach(self._record("om_b", "user-bob"))
        self.svc.save_outreach(self._record("om_legacy", None))
        rows, total = self.svc.list_outreach(owner_user_id="user-alice")
        ids = {r["outreach_id"] for r in rows}
        self.assertEqual(ids, {"om_a", "om_legacy"})
        self.assertEqual(total, 2)

    def test_list_unscoped_returns_everything(self):
        self.svc.save_outreach(self._record("om_a", "user-alice"))
        self.svc.save_outreach(self._record("om_b", "user-bob"))
        rows, total = self.svc.list_outreach()
        self.assertEqual(total, 2)

    def test_owner_is_not_clobbered_by_an_ownerless_update(self):
        self.svc.save_outreach(self._record("om_a", "user-alice"))
        update = {"outreach_id": "om_a", "email": "a@b.c",
                  "outreach_class": "email", "status": "sent",
                  "provider": "local"}
        self.svc.save_outreach(update)
        self.assertEqual(
            self.svc.get_outreach("om_a")["owner_user_id"], "user-alice")


class ServiceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = PersistenceService(os.path.join(self._tmp.name, "db.sqlite"))

    def tearDown(self):
        self.svc.close()
        self._tmp.cleanup()

    def test_create_stamps_owner(self):
        rec = outreach_service.create_outreach(
            self.svc, payload=_payload(), owner_user_id="user-alice")
        self.assertEqual(
            self.svc.get_outreach(rec["outreach_id"])["owner_user_id"],
            "user-alice")
        # Public projection must NOT leak the internal owner id.
        self.assertNotIn("owner_user_id", rec)

    def test_anonymous_create_leaves_owner_null(self):
        rec = outreach_service.create_outreach(self.svc, payload=_payload())
        self.assertIsNone(
            self.svc.get_outreach(rec["outreach_id"])["owner_user_id"])

    def test_get_scoped_to_owner(self):
        rec = outreach_service.create_outreach(
            self.svc, payload=_payload(), owner_user_id="user-alice")
        oid = rec["outreach_id"]
        self.assertIsNone(
            outreach_service.get_outreach(self.svc, oid,
                                          owner_user_id="user-bob"))
        self.assertIsNotNone(
            outreach_service.get_outreach(self.svc, oid,
                                          owner_user_id="user-alice"))


class DispatchOwnershipTests(unittest.TestCase):
    """The stdlib adapter enforces ownership using the bearer session."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = PersistenceService(os.path.join(self._tmp.name, "db.sqlite"))
        self.auth = AuthService(self.svc)
        self.alice = self.auth.create_user("alice@example.com")
        self.bob = self.auth.create_user("bob@example.com")
        self.alice_token = self.auth.create_session(
            self.alice["user_id"])["session_token"]
        self.bob_token = self.auth.create_session(
            self.bob["user_id"])["session_token"]

    def tearDown(self):
        self.svc.close()
        self._tmp.cleanup()

    def _headers(self, token):
        return {"authorization": f"Bearer {token}"}

    def _create(self, token):
        status, body = dispatch(
            self.svc, "POST", "/api/v1/outreach", {},
            json.dumps(_payload()).encode(), headers=self._headers(token))
        self.assertEqual(status, 201)
        return body["data"]["outreach_id"]

    def test_create_records_caller_as_owner(self):
        oid = self._create(self.alice_token)
        self.assertEqual(
            self.svc.get_outreach(oid)["owner_user_id"],
            self.alice["user_id"])

    def test_other_user_cannot_get_or_delete(self):
        oid = self._create(self.alice_token)
        status, body = dispatch(
            self.svc, "GET", f"/api/v1/outreach/{oid}", {},
            None, headers=self._headers(self.bob_token))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "outreach_not_found")
        status, _ = dispatch(
            self.svc, "DELETE", f"/api/v1/outreach/{oid}", {},
            None, headers=self._headers(self.bob_token))
        self.assertEqual(status, 404)
        # The record is untouched.
        self.assertIsNotNone(self.svc.get_outreach(oid))

    def test_owner_can_get(self):
        oid = self._create(self.alice_token)
        status, body = dispatch(
            self.svc, "GET", f"/api/v1/outreach/{oid}", {},
            None, headers=self._headers(self.alice_token))
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["outreach_id"], oid)

    def test_list_is_scoped(self):
        self._create(self.alice_token)
        self._create(self.bob_token)
        _, alice_body = dispatch(
            self.svc, "GET", "/api/v1/outreach", {},
            None, headers=self._headers(self.alice_token))
        ids = [r["outreach_id"] for r in alice_body["data"]["outreach"]]
        self.assertEqual(len(ids), 1)
        owner = self.svc.get_outreach(ids[0])["owner_user_id"]
        self.assertEqual(owner, self.alice["user_id"])


class MigrationTests(unittest.TestCase):
    def test_sqlite_schema_has_v15_with_owner_column(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 15)
        sql = {v: s for v, s in MIGRATIONS}[15]
        self.assertIn("ADD COLUMN owner_user_id", sql)
        self.assertIn("idx_outreach_owner", sql)

    def test_postgres_migration_file_mirrors_sqlite(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "database", "migrations", "postgres",
            "0014_outreach_owner.sql")
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        self.assertIn("ADD COLUMN IF NOT EXISTS owner_user_id", sql)
        self.assertIn("idx_outreach_owner", sql)

    def test_routes_are_registered(self):
        paths = {entry[2] for entry in ROUTE_TABLE}
        self.assertIn("/api/v1/outreach", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
