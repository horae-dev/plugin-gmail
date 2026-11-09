# SPDX-License-Identifier: Apache-2.0
"""`python -m horae_gmail google-auth`: the loopback consent against a fake token endpoint and
a fake browser, and its refusal of every scope but the two read-only ones. No network."""

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import CLIENT_ID, CLIENT_SECRET
from horae_gmail import consent
from horae_gmail.__main__ import main as cli
from horae_gmail.oauth import DEFAULT_SCOPE, SCOPE_METADATA, SCOPE_READONLY

REFRESH = "1//fake-refresh"
FORBIDDEN = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    f"{SCOPE_READONLY} https://www.googleapis.com/auth/gmail.modify",
    f"{SCOPE_READONLY} {SCOPE_METADATA}",
    f"{SCOPE_READONLY},https://mail.google.com/",
    "openid",
    "",
]


class Token(BaseHTTPRequestHandler):
    reply: tuple[int, dict[str, Any]] = (200, {"refresh_token": REFRESH})
    forms: list[dict[str, str]] = []  # noqa: RUF012

    def log_message(self, format: str, *args: object) -> None:
        """Silent."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        Token.forms.append({k: v[0] for k, v in parse_qs(body).items()})
        status, data = Token.reply
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def token_url() -> Iterator[str]:
    Token.reply = (200, {"refresh_token": REFRESH})
    Token.forms.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Token)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/token"
    httpd.shutdown()
    httpd.server_close()


def browser(answer: Callable[[dict[str, str]], dict[str, str]]) -> Callable[[str], None]:
    """A fake browser: reads the consent URL and calls the redirect URI back with `answer`'s
    query (given the URL's own: `state`...)."""
    seen: dict[str, str] = {}

    def open_url(url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        seen.update(query)
        back = answer(query)
        threading.Thread(
            target=httpx.get, args=(query["redirect_uri"],), kwargs={"params": back}
        ).start()

    open_url.seen = seen  # type: ignore[attr-defined]
    return open_url


def ok(q: dict[str, str]) -> dict[str, str]:
    return {"code": "the-code", "state": q["state"]}


def authorize(token_url: str, answer: Callable[[dict[str, str]], dict[str, str]], **kw: Any) -> str:
    return consent.authorize(
        CLIENT_ID,
        CLIENT_SECRET,
        open_url=browser(answer),
        token_url=token_url,
        wait_s=10,
        err=StringIO(),
        **kw,
    )


def test_consent_requests_only_the_default_read_only_scope(token_url: str) -> None:
    opener = browser(ok)
    err = StringIO()
    lines = consent.authorize(
        CLIENT_ID, CLIENT_SECRET, open_url=opener, token_url=token_url, wait_s=10, err=err
    )
    assert lines.splitlines() == [
        f"GMAIL_CLIENT_ID={CLIENT_ID}",
        f"GMAIL_CLIENT_SECRET={CLIENT_SECRET}",
        f"GMAIL_REFRESH_TOKEN={REFRESH}",
    ]
    asked = opener.seen  # type: ignore[attr-defined]
    assert asked["scope"] == DEFAULT_SCOPE == SCOPE_READONLY
    assert asked["access_type"] == "offline"
    assert asked["prompt"] == "consent"
    assert asked["code_challenge_method"] == "S256"
    assert asked["redirect_uri"].startswith("http://127.0.0.1:")
    (form,) = Token.forms
    assert form["code"] == "the-code"
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"]
    assert "the-code" not in err.getvalue()
    assert REFRESH not in err.getvalue()


@pytest.mark.parametrize("name", ["metadata", SCOPE_METADATA])
def test_the_metadata_scope_can_be_chosen(token_url: str, name: str) -> None:
    opener = browser(ok)
    consent.authorize(
        CLIENT_ID,
        CLIENT_SECRET,
        open_url=opener,
        scope=name,
        token_url=token_url,
        wait_s=10,
        err=StringIO(),
    )
    assert opener.seen["scope"] == SCOPE_METADATA  # type: ignore[attr-defined]


@pytest.mark.parametrize("scope", FORBIDDEN)
def test_any_other_scope_is_refused_before_anything_happens(token_url: str, scope: str) -> None:
    opened: list[str] = []
    with pytest.raises(consent.AuthError, match="scope refused"):
        consent.authorize(
            CLIENT_ID,
            CLIENT_SECRET,
            open_url=opened.append,
            scope=scope,
            token_url=token_url,
            wait_s=1,
            err=StringIO(),
        )
    assert opened == []
    assert not Token.forms


@pytest.mark.parametrize("scope", FORBIDDEN)
def test_the_command_refuses_other_scopes_before_asking_for_anything(
    scope: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked for the id"))
    with pytest.raises(SystemExit, match="scope refused"):
        cli(["google-auth", "--scope", scope])
    assert capsys.readouterr().out == ""


def test_the_scope_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_SCOPE", "https://www.googleapis.com/auth/gmail.modify")
    with pytest.raises(SystemExit, match="scope refused"):
        cli(["google-auth"])


def test_a_scope_the_user_granted_beyond_the_request_is_refused(token_url: str) -> None:
    Token.reply = (200, {"refresh_token": REFRESH, "scope": f"{SCOPE_READONLY} openid"})
    with pytest.raises(consent.AuthError, match="other than the one requested"):
        authorize(token_url, ok)


def test_the_granted_scope_may_be_the_requested_one(token_url: str) -> None:
    Token.reply = (200, {"refresh_token": REFRESH, "scope": SCOPE_READONLY})
    assert authorize(token_url, ok).endswith(f"GMAIL_REFRESH_TOKEN={REFRESH}")


def test_a_wrong_state_is_refused(token_url: str) -> None:
    with pytest.raises(consent.AuthError, match="state"):
        authorize(token_url, lambda q: {"code": "c", "state": "forged"})
    assert not Token.forms


def test_a_refusal_by_the_user(token_url: str) -> None:
    with pytest.raises(consent.AuthError, match="access_denied"):
        authorize(token_url, lambda q: {"error": "access_denied", "state": q["state"]})


def test_an_answer_without_a_code(token_url: str) -> None:
    with pytest.raises(consent.AuthError, match="no authorisation code"):
        authorize(token_url, lambda q: {"state": q["state"]})


def test_no_refresh_token_explains_how_to_get_one(token_url: str) -> None:
    Token.reply = (200, {"access_token": "x"})
    with pytest.raises(consent.AuthError, match="permissions"):
        authorize(token_url, ok)


def test_a_refused_code(token_url: str) -> None:
    Token.reply = (400, {"error": "invalid_grant"})
    with pytest.raises(consent.AuthError, match="invalid_grant"):
        authorize(token_url, ok)


def test_timeout_when_nobody_answers(token_url: str) -> None:
    with pytest.raises(consent.AuthError, match="timed out"):
        consent.authorize(
            CLIENT_ID,
            CLIENT_SECRET,
            open_url=lambda url: None,
            token_url=token_url,
            wait_s=0.2,
            err=StringIO(),
        )


def test_an_unreachable_token_endpoint() -> None:
    with pytest.raises(consent.AuthError, match="could not reach"):
        consent.authorize(
            CLIENT_ID,
            CLIENT_SECRET,
            open_url=browser(ok),
            token_url="http://127.0.0.1:9/token",  # noqa: S106 (a port nobody listens on)
            wait_s=10,
            err=StringIO(),
        )


def test_the_command_prints_only_the_secrets_on_stdout(
    token_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.delenv("GMAIL_SCOPE", raising=False)
    real = consent.authorize
    fake_browser = browser(ok)

    def authorize_here(*args: Any, **kwargs: Any) -> str:
        return real(*args, **{**kwargs, "open_url": fake_browser, "token_url": token_url})

    monkeypatch.setattr(consent, "authorize", authorize_here)
    cli(["google-auth", "--no-browser", "--scope", "metadata"])
    out, err = capsys.readouterr()
    assert out == (
        f"GMAIL_CLIENT_ID={CLIENT_ID}\nGMAIL_CLIENT_SECRET={CLIENT_SECRET}\n"
        f"GMAIL_REFRESH_TOKEN={REFRESH}\n"
    )
    assert REFRESH not in err
    assert fake_browser.seen["scope"] == SCOPE_METADATA  # type: ignore[attr-defined]


def test_the_command_fails_cleanly(token_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise consent.AuthError("nope")

    monkeypatch.setattr(consent, "authorize", refuse)
    with pytest.raises(SystemExit, match="google-auth: nope"):
        cli(["google-auth"])
    for bad in (["google-auth", "--what"], ["google-auth", "--scope"], ["other"]):
        with pytest.raises(SystemExit, match="usage"):
            cli(bad)


def test_blank_credentials_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "two words")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    with pytest.raises(SystemExit, match="required"):
        cli(["google-auth"])
