# SPDX-License-Identifier: Apache-2.0
"""Google OAuth for the plugin: the scopes it may use and the access-token lifecycle.

**The credentials are three secrets** (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`,
`GMAIL_REFRESH_TOKEN`, see `Credentials`): an OAuth client id, its client secret and a
long-lived refresh token, from the one-time consent that `python -m horae_gmail google-auth`
runs on the user's machine (never in the server). A call exchanges the refresh token at the
token endpoint for a short-lived access token, held in memory for that call only
(`AccessTokens`). A 401 from the API makes one fresh exchange and one retry (`gmail.py`).
Nothing is written to disk, logged, returned or put in an error: every failure is a
`GoogleError` with a fixed `reason`, never Google's message.

**Scopes.** Only read-only Gmail scopes exist in this plugin (`READ_ONLY_SCOPES`). Modify,
compose, send and full-mailbox scopes are not named anywhere, and an access token whose
granted scopes include any other scope is refused (`scope_not_read_only`).
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from horae_gmail.fetch import Api

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an endpoint, not a secret)
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_METADATA = "https://www.googleapis.com/auth/gmail.metadata"
READ_ONLY_SCOPES = (SCOPE_READONLY, SCOPE_METADATA)
"""The only scopes the consent helper will request and the plugin will accept."""
DEFAULT_SCOPE = SCOPE_READONLY
"""Why `gmail.readonly` and not `gmail.metadata` (README, "Which scope"): the triage of #3
needs message bodies for mail that rules do not decide, and `gmail.metadata` can neither read
them nor use the `q` search parameter. This story's source reads headers only."""

CLIENT_ID_SECRET = "GMAIL_CLIENT_ID"  # noqa: S105 (a secret's name, not its value)
CLIENT_SECRET_SECRET = "GMAIL_CLIENT_SECRET"  # noqa: S105 (a secret's name, not its value)
REFRESH_TOKEN_SECRET = "GMAIL_REFRESH_TOKEN"  # noqa: S105 (a secret's name, not its value)
SECRETS = (CLIENT_ID_SECRET, CLIENT_SECRET_SECRET, REFRESH_TOKEN_SECRET)

AUTH_FAILED_MESSAGE = (
    "auth_failed: Google refused the stored credentials; "
    "run `python -m horae_gmail google-auth` again and update the GMAIL_* secrets"
)
"""A fixed message: it never holds anything Google sent."""
_AUTH_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client"}


class GoogleError(Exception):
    """A call that cannot succeed; `reason` is a fixed code (`auth_failed`, `rate_limited`,
    `bad_response`...), never Google's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Credentials:
    """The three `GMAIL_*` secrets. Its `repr` hides everything."""

    client_id: str
    client_secret: str
    refresh_token: str

    def __repr__(self) -> str:
        return "Credentials(<hidden>)"


def credentials(environ: dict[str, str] | Any) -> Credentials | None:
    """The credentials of the environment; `None` when one secret is missing or empty."""
    values = [str(environ.get(name, "")).strip() for name in SECRETS]
    if not all(values) or any(len(value.split()) != 1 for value in values):
        return None
    return Credentials(*values)


class AccessTokens:
    """The access token of one account for the length of one call: exchanged on first use,
    replaced once when the API refuses it. It lives in this object only."""

    def __init__(self, api: Api, credentials: Credentials) -> None:
        self._api = api
        self._credentials = credentials
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def get(self, refused: str | None = None) -> str:
        """The token; a fresh one when `refused` (the token the API just rejected) is still
        the current one."""
        async with self._lock:
            if self._token is None or self._token == refused:
                self._token = await exchange(self._api, self._credentials)
            return self._token


async def exchange(api: Api, credentials: Credentials) -> str:
    """An access token for the refresh token (`GoogleError` `auth_failed` when Google refuses
    it: revoked, expired, or an app in "Testing" whose refresh tokens last 7 days)."""
    body = urlencode(
        {
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    status, answer = await api.api(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    data = parse_json(answer)
    if status == 200:
        token = data.get("access_token")
        if not (isinstance(token, str) and token and not any(c.isspace() for c in token)):
            raise GoogleError("bad_response")
        granted = data.get("scope")
        if isinstance(granted, str) and not set(granted.split()) <= set(READ_ONLY_SCOPES):
            raise GoogleError("scope_not_read_only")
        return token
    if status in (400, 401) and data.get("error") in _AUTH_ERRORS:
        raise GoogleError("auth_failed")
    raise GoogleError("rate_limited" if status == 429 else f"token_http_{status}")


def parse_json(raw: bytes) -> dict[str, Any]:
    """The JSON object in `raw`; `{}` for anything else."""
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
