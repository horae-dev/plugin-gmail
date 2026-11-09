# SPDX-License-Identifier: Apache-2.0
"""The token lifecycle: the refresh flow, one refresh and retry on a 401, `invalid_grant` as
`auth_failed` with a fixed message, and the credentials and scopes it accepts."""

from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed, call

from conftest import (
    ACCESS,
    APPROVED,
    CLIENT_ID,
    CLIENT_SECRET,
    ENVIRON,
    REFRESH,
    FakeGoogle,
    environ_without,
    no_leak,
    recorded,
)
from horae_gmail import build, oauth


def run(plugin: Plugin, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call(plugin, "gmail.recent", params, network=APPROVED)

    return anyio.run(go)


def test_the_refresh_token_is_exchanged_for_an_access_token(google: FakeGoogle) -> None:
    replayed = run(build(google, environ=ENVIRON), max_results=1)
    assert replayed.error is None
    first = google.seen[0]
    assert first["route"] == "token"
    assert first["form"] == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH,
        "grant_type": "refresh_token",
    }
    # the access token is used as a bearer token, once exchanged, for every Gmail call
    assert [e["auth"] for e in google.seen[1:]] == [f"Bearer {ACCESS}"] * (len(google.seen) - 1)
    assert google.routes().count("token") == 1


def test_a_401_makes_one_refresh_and_one_retry(google: FakeGoogle) -> None:
    fresh = b'{"access_token": "ya29.fake-second-token", "expires_in": 3599}'
    google.script = {
        "list": [(401, recorded("unauthorized")), (200, recorded("messages-list"))],
        "token": [(200, recorded("token")), (200, fresh)],
    }
    replayed = run(build(google, environ=ENVIRON), max_results=1)
    assert replayed.error is None
    assert isinstance(replayed.data, dict)
    assert replayed.data["count"] == 1
    assert google.routes()[:4] == ["token", "list", "token", "list"]
    assert google.seen[1]["auth"] == f"Bearer {ACCESS}"
    assert google.seen[3]["auth"] == "Bearer ya29.fake-second-token"
    assert google.routes().count("token") == 2


def test_a_second_401_is_auth_failed_without_another_refresh(google: FakeGoogle) -> None:
    google.script = {"list": [(401, recorded("unauthorized"))] * 3}
    replayed = run(build(google, environ=ENVIRON))
    assert replayed.error == oauth.AUTH_FAILED_MESSAGE
    assert google.routes() == ["token", "list", "token", "list"]


def test_invalid_grant_is_auth_failed_with_a_fixed_message(google: FakeGoogle) -> None:
    google.script = {"token": [(400, recorded("invalid-grant"))]}
    replayed = run(build(google, environ=ENVIRON))
    assert replayed.error == oauth.AUTH_FAILED_MESSAGE
    assert replayed.error is not None
    assert replayed.error.startswith("auth_failed")
    assert "google-auth" in replayed.error
    assert "expired or revoked" not in replayed.error  # Google's text never reaches the user
    assert google.routes() == ["token"]  # nothing else is called with a dead grant


@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
def test_a_refused_client_is_auth_failed(google: FakeGoogle, error: str) -> None:
    google.script = {"token": [(401, f'{{"error": "{error}"}}'.encode())]}
    assert run(build(google, environ=ENVIRON)).error == oauth.AUTH_FAILED_MESSAGE


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (429, b"{}", "rate_limited"),
        (400, b'{"error": "invalid_request"}', "token_http_400"),
        (200, b"{}", "bad_response"),
        (200, b'{"access_token": "has space"}', "bad_response"),
        (200, b"not json", "bad_response"),
    ],
)
def test_other_token_failures_are_fixed_codes(
    google: FakeGoogle, status: int, body: bytes, reason: str
) -> None:
    google.script = {"token": [(status, body)]}
    assert run(build(google, environ=ENVIRON)).error == reason


def test_a_token_with_a_broader_scope_is_refused(google: FakeGoogle) -> None:
    modify = b'{"access_token": "ya29.x", "scope": "https://www.googleapis.com/auth/gmail.modify"}'
    google.script = {"token": [(200, modify)]}
    assert run(build(google, environ=ENVIRON)).error == "scope_not_read_only"
    assert google.routes() == ["token"]


def test_both_read_only_scopes_are_accepted(google: FakeGoogle) -> None:
    both = f"{oauth.SCOPE_METADATA} {oauth.SCOPE_READONLY}"
    body = f'{{"access_token": "ya29.x", "scope": "{both}"}}'.encode()
    google.script = {"token": [(200, body)]}
    assert run(build(google, environ=ENVIRON), max_results=1).error is None


def test_no_modify_or_send_scope_exists_in_the_source() -> None:
    assert oauth.DEFAULT_SCOPE == oauth.SCOPE_READONLY
    assert set(oauth.READ_ONLY_SCOPES) == {oauth.SCOPE_READONLY, oauth.SCOPE_METADATA}
    from pathlib import Path

    source = "".join(p.read_text("utf-8") for p in Path(oauth.__file__).parent.glob("*.py"))
    for word in ("gmail.modify", "gmail.send", "gmail.compose", "gmail.insert"):
        assert word not in source, word
    # The full-mailbox scope is the bare `https://mail.google.com/`; the triage's task link
    # (`https://mail.google.com/mail/u/0/#all/<id>`) is a link, never requested or contacted.
    for quote in ('"', "'"):
        assert f"{quote}https://mail.google.com/{quote}" not in source


@pytest.mark.parametrize(
    "missing", ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"]
)
def test_a_missing_secret_is_named_not_echoed(google: FakeGoogle, missing: str) -> None:
    replayed = run(build(google, environ=environ_without(missing)))
    assert replayed.error is not None
    assert replayed.error.startswith("secret_missing:")
    assert missing in replayed.error
    no_leak(replayed.error)
    assert google.seen == []


def test_a_secret_with_spaces_is_refused(google: FakeGoogle) -> None:
    environ = {**ENVIRON, "GMAIL_REFRESH_TOKEN": "two words"}
    assert run(build(google, environ=environ)).error is not None
    assert google.seen == []


def test_credentials_hide_themselves() -> None:
    found = oauth.credentials(ENVIRON)
    assert found is not None
    assert repr(found) == "Credentials(<hidden>)"
    no_leak(repr(found), str(found))
