# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a fake Google answering from recorded, synthetic API responses
(`tests/fixtures/google/*.json`, never the network) and fake secrets."""

import base64
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
LABELS = [
    {"id": "INBOX", "name": "INBOX"},
    {"id": "CATEGORY_PROMOTIONS", "name": "CATEGORY_PROMOTIONS"},
    {"id": "Label_101", "name": "Receipts"},
    {"id": "Label_102", "name": "Clients/Acme"},
    {"id": "Label_103", "name": "Clients/Globex"},
]
APPROVED = ["gmail.googleapis.com", "oauth2.googleapis.com"]


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def mail(
    message_id: str,
    *,
    sender: str = "Ada Example <ada@example.org>",
    subject: str = "Hello",
    body: str | None = "Hi there",
    html: str | None = None,
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
    extra: tuple[tuple[str, str], ...] = (),
    attachment: bool = False,
    millis: str = "1793692800000",
) -> dict[str, Any]:
    """A synthetic `messages.get` answer with `format=full`: headers, a plain-text part, an
    optional HTML part and an optional attachment part (the real API nests them the same way)."""
    parts: list[dict[str, Any]] = []
    if body is not None:
        parts.append(
            {
                "partId": "0",
                "mimeType": "text/plain",
                "filename": "",
                "headers": [{"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}],
                "body": {"size": len(body), "data": b64(body)},
            }
        )
    if html is not None:
        parts.append(
            {
                "partId": "1",
                "mimeType": "text/html",
                "filename": "",
                "body": {"size": len(html), "data": b64(html)},
            }
        )
    if attachment:
        parts.append(
            {
                "partId": "2",
                "mimeType": "application/pdf",
                "filename": "scan.pdf",
                "body": {"attachmentId": "ANGjdJ-fake", "size": 123456},
            }
        )
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 02 Nov 2026 08:15:00 +0100"},
        *({"name": n, "value": v} for n, v in extra),
    ]
    return {
        "id": message_id,
        "threadId": message_id,
        "labelIds": list(labels),
        "internalDate": millis,
        "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts},
    }


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
        self.mailbox: dict[str, dict[str, Any]] = {}
        """Full messages (`format=full`) by id; the triage tests fill it."""
        self.history: list[dict[str, Any]] = []
        self.profile_id = "5000"
        self.latest_id = "5000"
        self.labels: list[dict[str, str]] = list(LABELS)
        """The mailbox's labels (`labels.list`), synthetic."""

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
        if route == "profile":
            return 200, json.dumps({"historyId": self.profile_id}).encode()
        if route == "labels":
            return 200, json.dumps({"labels": self.labels}).encode()
        if route == "history":
            answer: dict[str, Any] = {"history": self.history, "historyId": self.latest_id}
            return 200, json.dumps(answer).encode()
        if route == "list":
            return 200, recorded("messages-list")
        message_id = route.removeprefix("get:")
        if parse_qs(parts.query).get("format") == ["full"]:
            if message_id in self.mailbox:
                return 200, json.dumps(self.mailbox[message_id]).encode()
            return 404, recorded("not-found")
        if (GOOGLE / f"message-{message_id}.json").exists():
            return 200, recorded(f"message-{message_id}")
        return 404, recorded("not-found")

    @staticmethod
    def route(method: str, host: str, path: str) -> str:
        if host == "oauth2.googleapis.com" and method == "POST" and path == "/token":
            return "token"
        assert host == "gmail.googleapis.com", host
        assert method == "GET"
        if path == "/gmail/v1/users/me/profile":
            return "profile"
        if path == "/gmail/v1/users/me/labels":
            return "labels"
        if path == "/gmail/v1/users/me/history":
            return "history"
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
