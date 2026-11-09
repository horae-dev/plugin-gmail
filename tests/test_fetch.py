# SPDX-License-Identifier: Apache-2.0
"""The HTTP client against a fake Google on 127.0.0.1 (never the network): the whole token
and Gmail flow through `HttpFetcher`, its bounds (no redirect, timeouts, size), the host check
and the logs."""

import json
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

import anyio
import pytest
from horae_sdk.testing import Replayed, call

from conftest import ACCESS, ENVIRON, SENSITIVE, no_leak, recorded
from horae_gmail import build, gmail, oauth
from horae_gmail.fetch import FetchError, HttpFetcher, check_url

Reply = tuple[int, bytes]


class Server(BaseHTTPRequestHandler):
    """Plays queued replies per route (the last repeats); records every request."""

    queues: dict[str, deque[Reply]] = defaultdict(deque)  # noqa: RUF012
    seen: list[dict[str, Any]] = []  # noqa: RUF012
    delay = 0.0

    def log_message(self, format: str, *args: object) -> None:
        """Silent: the server must not write tokens to stderr."""

    def route(self) -> str:
        path = self.path.split("?")[0]
        if path == "/token":
            return "token"
        if path.endswith("/messages"):
            return "list"
        return "get"

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.answer({k: v[0] for k, v in parse_qs(body).items()})

    def do_GET(self) -> None:
        self.answer(None)

    def answer(self, form: dict[str, str] | None) -> None:
        route = self.route()
        Server.seen.append(
            {"route": route, "auth": self.headers.get("Authorization"), "form": form}
        )
        if Server.delay:
            time.sleep(Server.delay)
        queue = Server.queues[route]
        status, body = queue[0] if len(queue) == 1 else queue.popleft()
        self.send_response(status)
        if status == 307:
            self.send_header("Location", f"http://127.0.0.2:{self.server.server_port}/x")  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def fake(server: str, monkeypatch: pytest.MonkeyPatch) -> type[Server]:
    Server.queues.clear()
    Server.seen.clear()
    Server.delay = 0.0
    Server.queues["token"].append((200, recorded("token")))
    Server.queues["list"].append((200, recorded("messages-list")))
    Server.queues["get"].append((200, recorded("message-18f0a1b2c3d4e5f1")))
    monkeypatch.setattr(oauth, "TOKEN_URL", f"{server}/token")
    monkeypatch.setattr(gmail, "API_URL", f"{server}/gmail/v1/users/me/messages")
    # the host check is for the real hosts: 127.0.0.1 stands in for both of them here
    monkeypatch.setattr("horae_gmail.plugin.check_url", lambda url, hosts: url)
    return Server


def client(**kw: Any) -> HttpFetcher:
    return HttpFetcher(schemes=("http",), timeout_s=kw.pop("timeout_s", 5.0), **kw)


def run(fetcher: HttpFetcher, **params: Any) -> Replayed:
    async def go() -> Replayed:
        # network=None: the fake server is local http, never an approved https host
        return await call(build(fetcher, environ=ENVIRON), "gmail.recent", params, network=None)

    return anyio.run(go)


def test_the_flow_through_the_real_client(fake: type[Server]) -> None:
    replayed = run(client(attempts=1), max_results=1)
    assert replayed.error is None
    assert isinstance(replayed.data, dict)
    assert replayed.data["messages"][0]["subject"] == "Quarterly report draft"
    assert [s["route"] for s in fake.seen] == ["token", "list", "get"]
    assert fake.seen[0]["form"]["grant_type"] == "refresh_token"
    assert fake.seen[1]["auth"] == f"Bearer {ACCESS}"


def test_a_401_refreshes_once_and_retries_through_the_real_client(fake: type[Server]) -> None:
    fake.queues["list"].clear()
    fake.queues["list"].extend([(401, recorded("unauthorized")), (200, recorded("messages-list"))])
    fake.queues["token"].append((200, b'{"access_token": "ya29.fake-second"}'))
    assert run(client(attempts=1), max_results=1).error is None
    assert [s["route"] for s in fake.seen] == ["token", "list", "token", "list", "get"]
    assert fake.seen[3]["auth"] == "Bearer ya29.fake-second"


def test_invalid_grant_through_the_real_client(fake: type[Server]) -> None:
    fake.queues["token"].clear()
    fake.queues["token"].append((400, recorded("invalid-grant")))
    assert run(client(attempts=1)).error == oauth.AUTH_FAILED_MESSAGE


def test_a_redirect_is_never_followed(fake: type[Server]) -> None:
    fake.queues["list"].clear()
    fake.queues["list"].append((307, b""))
    assert run(client(attempts=1)).error == "redirect_refused"
    assert [s["route"] for s in fake.seen] == ["token", "list"]  # nothing after the 307


def test_a_slow_server_times_out_within_the_budget(fake: type[Server]) -> None:
    fake.delay = 1.5
    started = time.monotonic()
    error = run(client(attempts=1, timeout_s=0.5, read_timeout_s=0.3)).error
    assert error == "timeout"
    assert time.monotonic() - started < 3


def test_a_server_error_is_retried_once(fake: type[Server]) -> None:
    fake.queues["token"].clear()
    fake.queues["token"].extend([(503, b"{}"), (200, recorded("token"))])
    assert run(client(backoff_s=0.0), max_results=1).error is None
    assert [s["route"] for s in fake.seen].count("token") == 2


def test_an_unlisted_status_is_a_fixed_code(fake: type[Server]) -> None:
    fake.queues["list"].clear()
    fake.queues["list"].append((418, b"{}"))
    assert run(client(attempts=1)).error == "http_418"


def test_an_oversized_answer_is_refused(fake: type[Server]) -> None:
    assert run(client(attempts=1, max_bytes=10)).error == "too_large"


def test_the_http_loggers_stay_quiet(fake: type[Server], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        run(client(attempts=1), max_results=1)
    assert "httpx" not in caplog.text
    assert "/token" not in caplog.text
    no_leak(caplog.text)


async def api(fetcher: HttpFetcher, url: str) -> tuple[int, bytes]:
    return await fetcher.api("GET", url, headers={})


@pytest.mark.parametrize(
    "url",
    [
        "http://oauth2.googleapis.com/token",
        "https://evil.example.org/token",
        "https://oauth2.googleapis.com.evil.example.org/token",
        "https://user@oauth2.googleapis.com/token",
        "https://oauth2.googleapis.com:8443/token",
        "https://oauth2.googleapis.com/to ken",
        "",
    ],
)
def test_only_the_declared_hosts_over_https(url: str) -> None:
    with pytest.raises(FetchError):
        check_url(url, ("oauth2.googleapis.com", "gmail.googleapis.com"))


def test_the_declared_hosts_pass() -> None:
    hosts = ("oauth2.googleapis.com", "gmail.googleapis.com")
    assert check_url("https://gmail.googleapis.com/gmail/v1/users/me/messages?a=1", hosts) == (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages?a=1"
    )


def test_the_real_client_is_https_only() -> None:
    with pytest.raises(FetchError, match="not_https"):
        anyio.run(api, HttpFetcher(), "http://gmail.googleapis.com/x")


def test_a_failed_connection_is_a_fixed_code() -> None:
    fetcher = client(attempts=1)
    with pytest.raises(FetchError) as caught:
        anyio.run(api, fetcher, "http://127.0.0.1:9/x")
    assert caught.value.reason == "connect_failed"
    no_leak(*[str(a) for a in caught.value.args])
    assert not any(secret in repr(caught.value) for secret in SENSITIVE)


def test_the_json_helpers_survive_garbage() -> None:
    assert oauth.parse_json(b"[1]") == {}
    assert oauth.parse_json(json.dumps({"a": 1}).encode()) == {"a": 1}
