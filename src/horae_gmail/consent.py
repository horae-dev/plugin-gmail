# SPDX-License-Identifier: Apache-2.0
"""`python -m horae_gmail google-auth`: the one-time consent that yields the refresh token.

**This runs on your machine, once, and never in the plugin server.** It is the OAuth 2.0
installed-application flow with a loopback redirect (RFC 8252) and PKCE: it starts a server
on `127.0.0.1` on a free port, sends you to Google's consent screen, receives the
authorisation code on that port, exchanges it at the token endpoint and prints three lines on
**stdout** (everything else goes to stderr): `GMAIL_CLIENT_ID=...`, `GMAIL_CLIENT_SECRET=...`
and `GMAIL_REFRESH_TOKEN=...`, the three secrets. Nothing is stored by this command: redirect
stdout to the secret file yourself (README, "Run the consent helper").

The client id and secret come from `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, else are
asked for (the secret without echo).

**Scope.** Exactly one scope is requested: `--scope` (or `GMAIL_SCOPE`), by default
`gmail.readonly`. Only the two read-only scopes are accepted (`oauth.READ_ONLY_SCOPES`, by URL
or by the short names `readonly` and `metadata`); anything else, a modify, compose, send or
full-access scope, a list of scopes, is refused *before* a browser opens or anything is sent
to Google, and the granted scope Google reports back must be that one.
"""

import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import webbrowser
from collections.abc import Callable
from getpass import getpass
from typing import Any, TextIO
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from horae_gmail.oauth import (
    CLIENT_ID_SECRET,
    CLIENT_SECRET_SECRET,
    DEFAULT_SCOPE,
    READ_ONLY_SCOPES,
    REFRESH_TOKEN_SECRET,
    SCOPE_METADATA,
    SCOPE_READONLY,
    TOKEN_URL,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
WAIT_S = 300.0
LOOPBACK = "127.0.0.1"
ALIASES = {"readonly": SCOPE_READONLY, "metadata": SCOPE_METADATA}


class AuthError(Exception):
    """The consent did not complete; the message is safe to show (no code or token)."""


def check_scope(scope: str) -> str:
    """The full URL of a read-only scope (`AuthError` for any other: the text is never echoed
    back in full, only counted or named from the allow-list)."""
    wanted = ALIASES.get(scope.strip(), scope.strip())
    if wanted not in READ_ONLY_SCOPES:
        allowed = ", ".join(READ_ONLY_SCOPES)
        raise AuthError(f"scope refused: only one read-only scope is allowed ({allowed})")
    return wanted


def authorize(
    client_id: str,
    client_secret: str,
    *,
    open_url: Callable[[str], object],
    scope: str = DEFAULT_SCOPE,
    token_url: str = TOKEN_URL,
    wait_s: float = WAIT_S,
    err: TextIO | None = None,
) -> str:
    """Run the consent and return the three `GMAIL_*=value` lines. `open_url` is given the
    consent URL (a browser opener; the tests act as the browser)."""
    scope = check_scope(scope)
    err = err if err is not None else sys.stderr
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Silent: the request line holds the authorisation code."""

        def do_GET(self) -> None:
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if "state" in query or "code" in query or "error" in query:
                result.update(query)
                text = b"You can close this tab and go back to the terminal."
                done.set()
            else:
                text = b"Waiting for Google."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

    server = http.server.ThreadingHTTPServer((LOOPBACK, 0), Handler)
    server.daemon_threads = True
    redirect = f"http://{LOOPBACK}:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = (
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": scope,
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                    "code_challenge": challenge.rstrip(b"=").decode("ascii"),
                    "code_challenge_method": "S256",
                }
            )
        )
        err.write(f"Open this URL to give read-only access to your Gmail:\n\n{url}\n\n")
        err.flush()
        open_url(url)
        if not done.wait(wait_s):
            raise AuthError("timed out waiting for Google's answer")
    finally:
        server.shutdown()
        server.server_close()
    if not secrets.compare_digest(result.get("state", ""), state):
        raise AuthError("the answer does not match this request (state)")
    if "error" in result:
        raise AuthError(f"Google refused: {result['error'][:64]}")
    code = result.get("code")
    if not code:
        raise AuthError("no authorisation code in the answer")
    refresh = _exchange(client_id, client_secret, code, verifier, redirect, token_url, scope)
    return (
        f"{CLIENT_ID_SECRET}={client_id}\n"
        f"{CLIENT_SECRET_SECRET}={client_secret}\n"
        f"{REFRESH_TOKEN_SECRET}={refresh}"
    )


def _exchange(
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect: str,
    token_url: str,
    scope: str,
) -> str:
    try:
        response = httpx.post(
            token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect,
            },
            timeout=20.0,
            trust_env=False,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise AuthError("could not reach Google's token endpoint") from None
    data: Any
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not isinstance(data, dict):
        error = data.get("error") if isinstance(data, dict) else None
        raise AuthError(f"Google's token endpoint refused the code: {str(error)[:64]}")
    granted = data.get("scope")
    if isinstance(granted, str) and set(granted.split()) != {scope}:
        raise AuthError(
            "Google granted a scope other than the one requested: not storing the token; "
            "remove this app's access at https://myaccount.google.com/permissions"
        )
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh or any(c.isspace() for c in refresh):
        raise AuthError(
            "Google returned no refresh token: remove this app's access at "
            "https://myaccount.google.com/permissions and run the command again"
        )
    return refresh


def main(argv: list[str]) -> None:
    """The command: stdout gets the secret lines, stderr the instructions."""
    scope = os.environ.get("GMAIL_SCOPE") or DEFAULT_SCOPE
    browse = True
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--no-browser":
            browse = False
        elif arg == "--scope" and args:
            scope = args.pop(0)
        else:
            sys.exit("usage: python -m horae_gmail google-auth [--scope SCOPE] [--no-browser]")
    try:
        scope = check_scope(scope)  # before anything is asked or opened
    except AuthError as error:
        sys.exit(f"google-auth: {error}")
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or input("OAuth client id: ").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or getpass("OAuth client secret: ")
    if len(client_id.split()) != 1 or len(client_secret.split()) != 1:
        sys.exit("the client id and secret are required, without spaces")
    opener: Callable[[str], object] = webbrowser.open if browse else (lambda url: None)
    try:
        lines = authorize(client_id.strip(), client_secret.strip(), open_url=opener, scope=scope)
    except AuthError as error:
        sys.exit(f"google-auth: {error}")
    sys.stdout.write(lines + "\n")
