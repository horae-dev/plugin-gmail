# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a fake Google answering from recorded, synthetic API responses
(`tests/fixtures/google/*.json`, never the network) and fake secrets."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from horae_sdk import Plugin

from horae_gmail import build

HERE = Path(__file__).parent
GOOGLE = HERE / "fixtures" / "google"
CLIENT_ID = "1234-fake.apps.example.org"
CLIENT_SECRET = "GOCSPX-fake-client-secret"  # noqa: S105 (synthetic)
REFRESH = "1//fake-refresh-token"
ACCESS = "ya29.fake-access-token-fixture"
ENVIRON = {
    "GMAIL_CLIENT_ID": CLIENT_ID,
    "GMAIL_CLIENT_SECRET": CLIENT_SECRET,
    "GMAIL_REFRESH_TOKEN": REFRESH,
}
SENSITIVE = (CLIENT_SECRET, REFRESH, ACCESS)
APPROVED = ["gmail.googleapis.com", "oauth2.googleapis.com"]


def recorded(name: str) -> bytes:
    """A recorded API response."""
    return (GOOGLE / f"{name}.json").read_bytes()


def no_leak(*texts: str) -> None:
    for text in texts:
        for secret in SENSITIVE:
            assert secret not in text


class FakeGoogle:
    """The `Api` of the plugin: answers each request from the recordings, or from `script`
    (a queue of `(status, body)` per route, used before the recordings), and keeps every
    request it saw."""

    def __init__(self, script: Mapping[str, list[tuple[int, bytes]]] | None = None) -> None:
        self.script = {route: list(queue) for route, queue in (script or {}).items()}
        self.seen: list[dict[str, Any]] = []

    async def api(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        parts = urlsplit(url)
        route = self.route(method, parts.netloc, parts.path)
        self.seen.append(
            {
                "route": route,
                "url": url,
                "auth": headers.get("Authorization"),
                "form": {k: v[0] for k, v in parse_qs((body or b"").decode()).items()},
            }
        )
        queue = self.script.get(route)
        if queue:
            return queue.pop(0)
        if route == "token":
            return 200, recorded("token")
        if route == "list":
            return 200, recorded("messages-list")
        message_id = route.removeprefix("get:")
        if (GOOGLE / f"message-{message_id}.json").exists():
            return 200, recorded(f"message-{message_id}")
        return 404, recorded("not-found")

    @staticmethod
    def route(method: str, host: str, path: str) -> str:
        if host == "oauth2.googleapis.com" and method == "POST" and path == "/token":
            return "token"
        assert host == "gmail.googleapis.com", host
        assert method == "GET"
        prefix = "/gmail/v1/users/me/messages"
        assert path.startswith(prefix), path
        rest = path.removeprefix(prefix).strip("/")
        return f"get:{rest}" if rest else "list"

    def routes(self) -> list[str]:
        return [str(entry["route"]) for entry in self.seen]


def environ_without(*names: str) -> dict[str, str]:
    return {k: v for k, v in ENVIRON.items() if k not in names}


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def plugin(google: FakeGoogle) -> Plugin:
    return build(google, environ=ENVIRON)


def dumps(value: object) -> str:
    return json.dumps(value, default=str)
