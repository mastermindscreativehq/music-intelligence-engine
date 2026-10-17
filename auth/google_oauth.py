import os
import secrets
import string
from datetime import datetime, timedelta, timezone

from google_auth_oauthlib.flow import Flow

# Sign-in requests incremental authorization (include_granted_scopes), so when
# the Google account already granted other scopes (e.g. a prior Gmail connect)
# the token response carries a SUPERSET of the sign-in scopes. oauthlib treats
# any scope change as a hard error and raises a Warning, which the callback
# turns into "oauth_exchange_failed". The change is expected and harmless here.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

# RFC 7636 S256 code verifier: 43-128 characters from the unreserved set
# (ALPHA / DIGIT / "-" / "." / "_" / "~"). Kept in the oauth module so the
# verifier is generated BEFORE the state row is written — one atomic insert,
# no window where a state exists without its verifier.
_CODE_VERIFIER_CHARS = string.ascii_letters + string.digits + "-._~"

# Sign-in and Gmail-sending are DELIBERATELY separate authorizations:
#
# SIGNIN_SCOPES exchanges a Google identity for an operator session. It mints
# no Gmail access — an operator can sign in and use the console without ever
# granting mailbox permissions.
SIGNIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# GMAIL_SEND_SCOPES is the MINIMAL scope needed to send outreach FROM the
# operator's own Gmail account (there is no shared system mailbox). Requested
# only by the explicit, authenticated "Connect Gmail" flow — never by
# sign-in. gmail.readonly is intentionally NOT requested: sending does not
# require reading mail, and the smallest sufficient scope is the honest one.
GMAIL_SEND_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
]


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


class GoogleOAuth:
    def __init__(self):
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
            self._configured = False
        else:
            self._configured = True

    @property
    def configured(self):
        return self._configured

    def new_code_verifier(self, length: int = 64) -> str:
        """Mint a PKCE code verifier (RFC 7636 S256)."""
        return "".join(secrets.choice(_CODE_VERIFIER_CHARS)
                       for _ in range(length))

    def _flow(self, scopes):
        return Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI],
                }
            },
            scopes=list(scopes),
        )

    def get_auth_url(self, state: str, code_verifier: str | None = None,
                     scopes=None) -> tuple[str, str]:
        if not self._configured:
            raise RuntimeError("Google OAuth not configured")
        flow = self._flow(scopes or SIGNIN_SCOPES)
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        # Use the caller-supplied verifier (persisted with the state); only if
        # none is given does the library autogenerate one. authorization_url()
        # embeds the matching S256 challenge in the URL.
        if code_verifier is not None:
            flow.code_verifier = code_verifier
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            # Incremental authorization: a later Gmail-connect hop reuses the
            # sign-in grant and only asks for what is newly needed.
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return auth_url, flow.code_verifier

    def exchange_code(self, code: str, code_verifier: str | None = None,
                      scopes=None) -> dict:
        if not self._configured:
            raise RuntimeError("Google OAuth not configured")
        flow = self._flow(scopes or SIGNIN_SCOPES)
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        # Replay the verifier the authorization URL was minted with. Without
        # this, fetch_token sends no code_verifier and Google rejects the
        # exchange with "(invalid_grant) Missing code verifier".
        flow.code_verifier = code_verifier
        flow.fetch_token(code=code)
        creds = flow.credentials
        return {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
            "id_token": getattr(creds, "id_token", None),
            "scopes": list(creds.scopes) if creds.scopes else list(
                scopes or SIGNIN_SCOPES),
        }


# The route layer selects scopes off the class
# (``GoogleOAuth.SIGNIN_SCOPES`` / ``GoogleOAuth.GMAIL_SEND_SCOPES``); expose
# the module-level sets there too so both spellings resolve to the same list.
GoogleOAuth.SIGNIN_SCOPES = SIGNIN_SCOPES
GoogleOAuth.GMAIL_SEND_SCOPES = GMAIL_SEND_SCOPES
