import base64
import os
from email.mime.text import MIMEText

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class GmailSendError(Exception):
    """A send failure with a machine-readable ``code``.

    Codes let callers answer an actionable HTTP status instead of a blanket
    500:

    - ``no_connected_gmail``    -> the operator never connected a mailbox
    - ``gmail_reauth_required`` -> the stored grant expired or was revoked
    - ``gmail_scope_missing``   -> consent was given without the send scope
    - ``gmail_send_failed``     -> any other provider/transport failure
    """

    def __init__(self, message, code="gmail_send_failed"):
        super().__init__(message)
        self.code = code


def _missing_send_scope(account: dict) -> bool:
    """True only when granted scopes are recorded AND omit gmail.send.

    An empty/legacy scope column is unknown, not restrictive: let the API
    decide rather than refusing a send we cannot prove is unauthorized.
    """
    raw = (account.get("scopes") or "").strip()
    if not raw:
        return False
    granted = [s.strip() for s in raw.split(",") if s.strip()]
    return GMAIL_SEND_SCOPE not in granted


class GmailProvider:
    """Send outreach from the operator's OWN connected Gmail account.

    Account/token persistence is delegated to the storage backend's
    ``auth_*_google_account`` methods (see :mod:`database.auth_store`), which
    speak both SQLite and PostgreSQL, so the send path works in production.
    """

    def __init__(self, storage):
        self.storage = storage

    def get_credentials(self, user_id: str):
        account = self.storage.auth_get_google_account(user_id)
        if not account:
            return None
        creds = Credentials(
            token=account.get("access_token"),
            refresh_token=account.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ.get("GOOGLE_CLIENT_ID"),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
            scopes=account["scopes"].split(",") if account.get("scopes")
            else None,
        )
        return creds, account

    def connected_account(self, user_id: str) -> dict | None:
        return self.storage.auth_get_google_account(user_id)

    def disconnect_account(self, user_id: str) -> int:
        # Drop the operator's stored Google credentials so a later send fails
        # closed with "no_connected_gmail" until they reconnect. The user row
        # and their session are untouched; disconnecting Gmail is not a
        # logout.
        return self.storage.auth_delete_google_account(user_id)

    def send_email(self, user_id: str, to: str, subject: str, body: str) -> dict:
        # The sender is ALWAYS the operator's connected Gmail account. A
        # caller-supplied From would let anyone forge the heading; there is no
        # shared system mailbox.
        result = self.get_credentials(user_id)
        if not result:
            raise GmailSendError("no_connected_gmail", code="no_connected_gmail")
        creds, account = result
        if _missing_send_scope(account):
            raise GmailSendError(
                "the connected Google account was not granted the Gmail send "
                "permission", code="gmail_scope_missing")
        try:
            service = build("gmail", "v1", credentials=creds,
                            cache_discovery=False)
            message = MIMEText(body, "plain")
            message["to"] = to
            message["subject"] = subject
            message["from"] = account["email"]
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
            sent = service.users().messages().send(
                userId="me", body={"raw": raw}).execute()
            return {
                "message_id": sent.get("id"),
                "thread_id": sent.get("threadId"),
                "sender_email": account["email"],
            }
        except RefreshError as e:
            # The stored refresh token no longer works (revoked, expired, or
            # the grant was removed in the Google account).
            raise GmailSendError(
                f"Google authorization expired or was revoked: {e}",
                code="gmail_reauth_required") from e
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            text = str(e)
            lowered = text.lower()
            if status == 401:
                raise GmailSendError(
                    f"Google authorization is no longer valid: {text}",
                    code="gmail_reauth_required") from e
            if status == 403 and ("insufficient" in lowered
                                  or "scope" in lowered
                                  or "permission" in lowered):
                raise GmailSendError(
                    f"Gmail send permission is missing: {text}",
                    code="gmail_scope_missing") from e
            raise GmailSendError(text, code="gmail_send_failed") from e
        except Exception as e:
            raise GmailSendError(str(e), code="gmail_send_failed") from e
