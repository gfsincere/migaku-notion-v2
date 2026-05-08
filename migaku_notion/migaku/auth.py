"""Firebase authentication for Migaku.

The Migaku backend uses Google Firebase Auth's standard email-password
flow. Two endpoints (both query-keyed by Migaku's public Firebase web
API key, identical to the constant migoku itself uses):

1. POST identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<APIKEY>
       body (JSON): {email, password, returnSecureToken: true}
       returns:    {refreshToken, idToken, expiresIn, localId, ...}

2. POST securetoken.googleapis.com/v1/token?key=<APIKEY>
       body (form-urlencoded): grant_type=refresh_token&refresh_token=<...>
       returns: {access_token, expires_in, refresh_token, ...}

Notes from wiring (Phase 1):
  * `expiresIn` (sign-in) and `expires_in` (refresh) are *strings* in
    the JSON response, not numbers. We coerce defensively.
  * Refresh returns a fresh `refresh_token` too (Google's idToken
    rotation policy). Always persist whatever the latest call gave
    us, not the original.
  * Live-traffic sanity check: the Chrome HAR redacts `Authorization`,
    so we can't see the real header inline. The Go reference
    (migoku/migaku_api.go::doAuthorizedJSONRequest) sends
    `Authorization: Bearer <idToken>` and the new core-server.migaku.com
    backend accepts that — confirmed against migoku's working code path.
    If a future Migaku deploy starts requiring an additional header,
    add it here in `AuthSession.bearer_headers()`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from . import const


log = logging.getLogger("migaku-notion")


# Refresh proactively when the id_token has fewer than this many seconds
# left. Mirrors the Go code's "5 second guard" on idToken expiry, but
# bumped to 60s to comfortably cover slow first-call round-trips.
REFRESH_BUFFER_SECONDS = 60


@dataclass
class AuthToken:
    """Firebase auth token pair (long-lived refresh + short-lived id_token)."""

    id_token: str
    refresh_token: str
    expires_at: float    # epoch seconds — set REFRESH_BUFFER_SECONDS before real expiry

    @property
    def is_fresh(self) -> bool:
        return self.id_token != "" and time.time() < self.expires_at


# Backwards-compat alias for the older name used in stub docstrings.
FirebaseAuthToken = AuthToken


def sign_in_with_password(email: str, password: str) -> AuthToken:
    """Initial login. Mirrors `migoku_api.go::TryFromEmailPassword`."""
    if not (email and password):
        raise ValueError("email and password are required")
    url = f"{const.FIREBASE_SIGN_IN_URL}?key={const.MIGAKU_API_KEY}"
    resp = requests.post(
        url,
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=30,
    )
    if not resp.ok:
        # Firebase returns a structured error body; surface it verbatim
        # so the user can act on it (EMAIL_NOT_FOUND, INVALID_PASSWORD, ...).
        raise RuntimeError(f"Migaku login failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    expires_in = _coerce_seconds(data.get("expiresIn"))
    return AuthToken(
        id_token=data["idToken"],
        refresh_token=data["refreshToken"],
        expires_at=time.time() + max(expires_in - REFRESH_BUFFER_SECONDS, 1),
    )


def refresh(refresh_token: str) -> AuthToken:
    """Refresh the id_token using the long-lived refresh_token.

    Mirrors `migoku_api.go::FirebaseAuthToken.refreshLocked`. The
    `/v1/token` endpoint specifically expects a FORM-ENCODED body, not
    JSON.
    """
    if not refresh_token:
        raise ValueError("refresh_token is required")
    url = f"{const.FIREBASE_REFRESH_URL}?key={const.MIGAKU_API_KEY}"
    resp = requests.post(
        url,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Migaku token refresh failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    expires_in = _coerce_seconds(data.get("expires_in"))
    return AuthToken(
        id_token=data["access_token"],
        refresh_token=data.get("refresh_token") or refresh_token,
        expires_at=time.time() + max(expires_in - REFRESH_BUFFER_SECONDS, 1),
    )


def ensure_fresh(token: AuthToken) -> AuthToken:
    """Return `token` unchanged if still fresh; else refresh it."""
    if token.is_fresh:
        return token
    return refresh(token.refresh_token)


def _coerce_seconds(value: object) -> int:
    """Firebase returns expiresIn / expires_in as a string. Be defensive."""
    if value is None:
        return 3600
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 3600


# ---------------------------------------------------------------------------
# AuthSession — stateful wrapper that holds the current token and refreshes
# transparently. One instance per `sync` invocation.
# ---------------------------------------------------------------------------

class AuthSession:
    """Holds the currently-valid `AuthToken` and refreshes on demand.

    Construct via `AuthSession.from_refresh_token(...)` for the common
    case (re-using a refresh token persisted to .env from a prior login),
    or `AuthSession.from_email_password(...)` for first-run.

    The `id_token` and `bearer_headers()` accessors auto-refresh if the
    token is within `REFRESH_BUFFER_SECONDS` of expiry, so callers
    inside a long-running sync don't need to think about token rotation.
    """

    def __init__(self, token: AuthToken) -> None:
        self._token = token

    # --- factories ------------------------------------------------------

    @classmethod
    def from_email_password(cls, email: str, password: str) -> "AuthSession":
        return cls(sign_in_with_password(email, password))

    @classmethod
    def from_refresh_token(cls, refresh_token: str) -> "AuthSession":
        return cls(refresh(refresh_token))

    # --- accessors ------------------------------------------------------

    @property
    def token(self) -> AuthToken:
        if not self._token.is_fresh:
            self._token = refresh(self._token.refresh_token)
        return self._token

    @property
    def id_token(self) -> str:
        return self.token.id_token

    @property
    def refresh_token(self) -> str:
        # Don't trigger a refresh just to read the long-lived token —
        # callers that want to persist this to .env should do so right
        # after construction or after force_refresh().
        return self._token.refresh_token

    def bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.id_token}"}

    def force_refresh(self) -> AuthToken:
        self._token = refresh(self._token.refresh_token)
        return self._token


# Convenience: build an AuthSession from a (refresh_token, email, password)
# tuple, picking whichever one is present. Used by the CLI for the common
# "prefer refresh token over re-deriving from password" path.
def auth_session_from_env(
    *,
    refresh_token: Optional[str] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> AuthSession:
    if refresh_token:
        try:
            return AuthSession.from_refresh_token(refresh_token)
        except RuntimeError as exc:
            log.warning("Refresh token rejected (%s); falling back to email+password.", exc)
    if email and password:
        return AuthSession.from_email_password(email, password)
    raise RuntimeError(
        "No usable Migaku credentials. Set MIGAKU_REFRESH_TOKEN (after running "
        "`python -m migaku_notion login`) or MIGAKU_EMAIL + MIGAKU_PASSWORD in .env."
    )
