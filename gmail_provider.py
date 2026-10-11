import base64
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class GmailSendError(Exception):
    pass


class GmailProvider:
    def __init__(self, storage):
        self.storage = storage

    def get_credentials(self, user_id: str):
        with self.storage._lock:
            row = self.storage._conn.execute(
                "SELECT * FROM user_google_accounts WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        creds = Credentials(
            token=row["access_token"],
            refresh_token=row["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=None,  # avoid needing client id if using existing; but we have it from env
            client_secret=None,
            scopes=row["scopes"].split(",") if row["scopes"] else None,
        )
        # Need client_id/secret for refresh; get from env
        import os

        creds.client_id = os.environ.get("GOOGLE_CLIENT_ID")
        creds.client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        return creds, dict(row)

    def connected_account(self, user_id: str) -> dict | None:
        with self.storage._lock:
            row = self.storage._conn.execute(
                "SELECT * FROM user_google_accounts WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def disconnect_account(self, user_id: str) -> int:
        # Drop the operator's stored Google credentials so a later send fails
        # closed with "no_connected_gmail" until they reconnect. The user row
        # and their session are untouched; disconnecting Gmail is not a
        # logout.
        with self.storage._lock, self.storage._conn:
            cursor = self.storage._conn.execute(
                "DELETE FROM user_google_accounts WHERE user_id=?",
                (user_id,),
            )
        return cursor.rowcount or 0

    def send_email(self, user_id: str, to: str, subject: str, body: str) -> dict:
        # The sender is ALWAYS the operator's connected Gmail account. A
        # caller-supplied From would let anyone forge the heading; there is no
        # shared system mailbox.
        result = self.get_credentials(user_id)
        if not result:
            raise GmailSendError("no_connected_gmail")
        creds, account = result
        try:
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            message = MIMEText(body, "plain")
            message["to"] = to
            message["subject"] = subject
            message["from"] = account["email"]
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return {
                "message_id": sent.get("id"),
                "thread_id": sent.get("threadId"),
                "sender_email": account["email"],
            }
        except HttpError as e:
            raise GmailSendError(str(e)) from e
        except Exception as e:
            raise GmailSendError(str(e)) from e
