import os
import uuid
from datetime import datetime, timedelta, timezone

from database.service import PersistenceService


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


class AuthError(Exception):
    pass


class AuthService:
    def __init__(self, storage: PersistenceService):
        self.storage = storage

    def create_user(self, email: str, name: str | None = None) -> dict:
        user_id = f"u_{uuid.uuid4().hex[:16]}"
        now = iso_now()
        with self.storage._lock, self.storage._conn:
            self.storage._conn.execute(
                "INSERT OR IGNORE INTO users (user_id, email, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email.lower(), name, now, now),
            )
            row = self.storage._conn.execute(
                "SELECT * FROM users WHERE email=?", (email.lower(),)
            ).fetchone()
        return dict(row) if row else {"user_id": user_id, "email": email.lower()}

    def get_user_by_email(self, email: str):
        with self.storage._lock:
            row = self.storage._conn.execute(
                "SELECT * FROM users WHERE email=?", (email.lower(),)
            ).fetchone()
        return dict(row) if row else None

    def create_session(self, user_id: str) -> dict:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        now = utc_now()
        created_at = now.isoformat()
        expires_at = (now + timedelta(days=30)).isoformat()
        with self.storage._lock, self.storage._conn:
            self.storage._conn.execute(
                "INSERT INTO user_sessions (session_token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, created_at, expires_at),
            )
        return {"session_token": token, "user_id": user_id, "expires_at": expires_at}

    def get_session(self, session_token: str):
        with self.storage._lock:
            row = self.storage._conn.execute(
                "SELECT * FROM user_sessions WHERE session_token=? AND revoked_at IS NULL AND expires_at > ?",
                (session_token, iso_now()),
            ).fetchone()
        return dict(row) if row else None

    def revoke_session(self, session_token: str):
        with self.storage._lock, self.storage._conn:
            self.storage._conn.execute(
                "UPDATE user_sessions SET revoked_at=? WHERE session_token=?",
                (iso_now(), session_token),
            )

    def create_oauth_state(self, user_id: str | None = None,
                           code_verifier: str | None = None) -> str:
        state = os.urandom(16).hex()
        now = utc_now()
        created_at = now.isoformat()
        expires_at = (now + timedelta(minutes=10)).isoformat()
        with self.storage._lock, self.storage._conn:
            self.storage._conn.execute(
                "INSERT INTO oauth_state (state, user_id, code_verifier, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (state, user_id, code_verifier, created_at, expires_at),
            )
        return state

    def consume_oauth_state(self, state: str) -> dict | None:
        with self.storage._lock:
            row = self.storage._conn.execute(
                "SELECT * FROM oauth_state WHERE state=?", (state,)
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            if record.get("consumed_at") or record.get("expires_at", "") <= iso_now():
                return None
        with self.storage._lock, self.storage._conn:
            self.storage._conn.execute(
                "UPDATE oauth_state SET consumed_at=? WHERE state=?",
                (iso_now(), state),
            )
        return record
