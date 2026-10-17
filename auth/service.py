import os
import uuid
from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


class AuthError(Exception):
    pass


class AuthService:
    """Operator sign-in: accounts, sessions, and OAuth CSRF state.

    Persistence is delegated to the storage backend's ``auth_*`` methods
    (see :mod:`database.auth_store`), which speak both SQLite and PostgreSQL,
    so sign-in works identically in local development and production.
    """

    def __init__(self, storage):
        self.storage = storage

    # -- users ---------------------------------------------------------------

    def create_user(self, email: str, name: str | None = None) -> dict:
        email = email.lower()
        user_id = f"u_{uuid.uuid4().hex[:16]}"
        now = iso_now()
        self.storage.auth_save_user({
            "user_id": user_id,
            "email": email,
            "name": name,
            "created_at": now,
            "updated_at": now,
        })
        # A pre-existing email wins; return the row that actually exists.
        row = self.storage.auth_get_user_by_email(email)
        return row or {"user_id": user_id, "email": email, "name": name}

    def get_user_by_email(self, email: str):
        return self.storage.auth_get_user_by_email(email.lower())

    def get_user_by_id(self, user_id: str):
        return self.storage.auth_get_user_by_id(user_id)

    def update_user_name(self, user_id: str, name: str | None):
        """Set the operator's display name and return the refreshed row.

        Only the name is operator-editable; the email is the identity key
        and is never changed here."""
        self.storage.auth_update_user_name(user_id, name, iso_now())
        return self.get_user_by_id(user_id)

    # -- sessions ------------------------------------------------------------

    def create_session(self, user_id: str) -> dict:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        now = utc_now()
        created_at = now.isoformat()
        expires_at = (now + timedelta(days=30)).isoformat()
        self.storage.auth_create_session({
            "session_token": token,
            "user_id": user_id,
            "created_at": created_at,
            "expires_at": expires_at,
        })
        return {"session_token": token, "user_id": user_id,
                "expires_at": expires_at}

    def get_session(self, session_token: str):
        return self.storage.auth_get_active_session(session_token, iso_now())

    def revoke_session(self, session_token: str):
        self.storage.auth_revoke_session(session_token, iso_now())

    # -- OAuth CSRF state ----------------------------------------------------

    def create_oauth_state(self, user_id: str | None = None,
                           code_verifier: str | None = None) -> str:
        state = os.urandom(16).hex()
        now = utc_now()
        self.storage.auth_create_oauth_state({
            "state": state,
            "user_id": user_id,
            "code_verifier": code_verifier,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
        })
        return state

    def consume_oauth_state(self, state: str):
        return self.storage.auth_consume_oauth_state(state, iso_now())
