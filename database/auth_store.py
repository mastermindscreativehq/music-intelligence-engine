"""Auth/user/session/OAuth persistence shared by both storage backends.

The Phase 9 auth vertical (operator sign-in, sessions, the Google/Gmail
connect flow) was originally written as ad-hoc SQLite SQL executed directly
against ``storage._conn``. That SQL only ran on SQLite: PostgreSQL (the
production backend) rejects ``?`` placeholders and ``INSERT OR IGNORE``, so
every sign-in / session / Gmail lookup raised. This module is the single
source of truth for those statements, expressed dialect-neutrally:

- the ONLY per-dialect difference is the parameter placeholder (``?`` for
  SQLite, ``%s`` for PostgreSQL), supplied by the host class via
  ``_AUTH_PLACEHOLDER``;
- every statement uses portable ``ON CONFLICT`` upserts and ``RETURNING``
  (supported by SQLite >= 3.35 and PostgreSQL), so ``users``,
  ``user_sessions``, ``oauth_state`` and ``user_google_accounts`` behave
  identically in development (SQLite) and production (PostgreSQL).

Rows are returned as plain dicts on both backends (SQLite via
``dict(sqlite3.Row)``, PostgreSQL already uses ``dict_row``).
"""

from __future__ import annotations


class AuthStoreMixin:
    """Backend-agnostic auth persistence; mixed into both storage backends.

    Subclasses provide ``_lock``, ``_conn`` and ``_AUTH_PLACEHOLDER``. The
    four ``_auth_*`` execution hooks default to the direct SQLite style;
    :class:`database.pg_store.PostgresStorage` overrides them to go through
    its recovering :meth:`~database.pg_store.PostgresStorage._guard`.
    """

    #: ``?`` for SQLite (qmark), ``%s`` for PostgreSQL (format).
    _AUTH_PLACEHOLDER = "?"

    # -- execution hooks (overridden by the PostgreSQL backend) --------------

    def _auth_read_one(self, sql, params=()):
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _auth_read_all(self, sql, params=()):
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _auth_write(self, sql, params=()) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(sql, params)
            return cur.rowcount

    def _auth_write_returning(self, sql, params=()):
        with self._lock, self._conn:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    # -- users ---------------------------------------------------------------

    def auth_save_user(self, user: dict) -> None:
        """Insert the operator row, ignoring an existing email (idempotent)."""
        ph = self._AUTH_PLACEHOLDER
        self._auth_write(
            "INSERT INTO users "
            "(user_id, email, name, created_at, updated_at) "
            f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}) "
            "ON CONFLICT DO NOTHING",
            (user["user_id"], user["email"], user.get("name"),
             user["created_at"], user["updated_at"]),
        )

    def auth_get_user_by_email(self, email: str):
        ph = self._AUTH_PLACEHOLDER
        return self._auth_read_one(
            f"SELECT * FROM users WHERE email={ph}", (email,))

    def auth_get_user_by_id(self, user_id: str):
        ph = self._AUTH_PLACEHOLDER
        return self._auth_read_one(
            f"SELECT * FROM users WHERE user_id={ph}", (user_id,))

    def auth_update_user_name(self, user_id: str, name, updated_at: str) -> None:
        """Update only the operator-owned display name (never the email)."""
        ph = self._AUTH_PLACEHOLDER
        self._auth_write(
            f"UPDATE users SET name={ph}, updated_at={ph} WHERE user_id={ph}",
            (name, updated_at, user_id))

    # -- sessions ------------------------------------------------------------

    def auth_create_session(self, session: dict) -> None:
        ph = self._AUTH_PLACEHOLDER
        self._auth_write(
            "INSERT INTO user_sessions "
            "(session_token, user_id, created_at, expires_at) "
            f"VALUES ({ph}, {ph}, {ph}, {ph})",
            (session["session_token"], session["user_id"],
             session["created_at"], session["expires_at"]),
        )

    def auth_get_active_session(self, token: str, now: str):
        """Return the session only while unrevoked and unexpired."""
        ph = self._AUTH_PLACEHOLDER
        return self._auth_read_one(
            "SELECT * FROM user_sessions "
            f"WHERE session_token={ph} "
            f"AND revoked_at IS NULL AND expires_at > {ph}",
            (token, now),
        )

    def auth_revoke_session(self, token: str, revoked_at: str) -> int:
        ph = self._AUTH_PLACEHOLDER
        return self._auth_write(
            f"UPDATE user_sessions SET revoked_at={ph} "
            f"WHERE session_token={ph}",
            (revoked_at, token),
        )

    # -- OAuth CSRF state ----------------------------------------------------

    def auth_create_oauth_state(self, state: dict) -> None:
        ph = self._AUTH_PLACEHOLDER
        self._auth_write(
            "INSERT INTO oauth_state "
            "(state, user_id, code_verifier, created_at, expires_at) "
            f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
            (state["state"], state.get("user_id"), state.get("code_verifier"),
             state["created_at"], state["expires_at"]),
        )

    def auth_get_oauth_state(self, state: str):
        ph = self._AUTH_PLACEHOLDER
        return self._auth_read_one(
            f"SELECT * FROM oauth_state WHERE state={ph}", (state,))

    def auth_consume_oauth_state(self, state: str, now: str):
        """Atomically consume a still-valid state row, else return ``None``.

        A single ``UPDATE ... RETURNING`` marks the row consumed and returns
        it only when it was unexpired and not already used, so a replayed
        ``state`` can never win a second time (no read-then-write race).
        """
        ph = self._AUTH_PLACEHOLDER
        return self._auth_write_returning(
            "UPDATE oauth_state SET consumed_at=" + ph +
            " WHERE state=" + ph +
            " AND consumed_at IS NULL AND expires_at > " + ph +
            " RETURNING *",
            (now, state, now),
        )

    # -- connected Google / Gmail accounts -----------------------------------

    def auth_get_google_account(self, user_id: str):
        ph = self._AUTH_PLACEHOLDER
        return self._auth_read_one(
            "SELECT * FROM user_google_accounts "
            f"WHERE user_id={ph} ORDER BY id DESC LIMIT 1",
            (user_id,),
        )

    def auth_find_google_account(self, google_id, user_id: str, email: str):
        """Find the account row by stable Google id, else by user + email."""
        ph = self._AUTH_PLACEHOLDER
        if google_id:
            row = self._auth_read_one(
                "SELECT * FROM user_google_accounts "
                f"WHERE google_id={ph} OR (user_id={ph} AND email={ph}) "
                "ORDER BY id DESC LIMIT 1",
                (google_id, user_id, email),
            )
            if row is not None:
                return row
        return self._auth_read_one(
            "SELECT * FROM user_google_accounts "
            f"WHERE user_id={ph} AND email={ph} ORDER BY id DESC LIMIT 1",
            (user_id, email),
        )

    def auth_save_google_account(self, account: dict) -> None:
        """Upsert the operator's connected Google account.

        On update, a fresh ``refresh_token``/``picture``/``google_id`` is
        written only when present (``COALESCE``), so a re-connect that omits
        the refresh token (Google only returns it on first consent) never
        erases the stored one.
        """
        ph = self._AUTH_PLACEHOLDER
        existing = self.auth_find_google_account(
            account.get("google_id"), account["user_id"], account["email"])
        if existing is None:
            self._auth_write(
                "INSERT INTO user_google_accounts "
                "(user_id, google_id, email, picture, access_token, "
                " refresh_token, token_expiry, scopes, created_at, updated_at) "
                f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, "
                f"{ph}, {ph})",
                (account["user_id"], account.get("google_id"),
                 account["email"], account.get("picture"),
                 account.get("access_token"), account.get("refresh_token"),
                 account.get("token_expiry"), account.get("scopes"),
                 account["created_at"], account["updated_at"]),
            )
            return
        self._auth_write(
            "UPDATE user_google_accounts SET "
            f"access_token={ph}, "
            f"refresh_token=COALESCE({ph}, refresh_token), "
            f"token_expiry={ph}, scopes={ph}, "
            f"picture=COALESCE({ph}, picture), "
            f"google_id=COALESCE({ph}, google_id), updated_at={ph} "
            f"WHERE id={ph}",
            (account.get("access_token"), account.get("refresh_token"),
             account.get("token_expiry"), account.get("scopes"),
             account.get("picture"), account.get("google_id"),
             account["updated_at"], existing["id"]),
        )

    def auth_delete_google_account(self, user_id: str) -> int:
        ph = self._AUTH_PLACEHOLDER
        return self._auth_write(
            f"DELETE FROM user_google_accounts WHERE user_id={ph}",
            (user_id,),
        )
