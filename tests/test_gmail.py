# SPDX-License-Identifier: Apache-2.0
"""`gmail.recent`: ids and three headers, no body; hostile headers escaped and capped; the
failures as fixed codes."""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed, call

from conftest import APPROVED, ENVIRON, FakeGoogle, recorded
from horae_gmail import build, gmail


def run(plugin: Plugin, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call(plugin, "gmail.recent", params, network=APPROVED)

    return anyio.run(go)


def messages(replayed: Replayed) -> list[dict[str, str]]:
    assert replayed.error is None, replayed.error
    assert isinstance(replayed.data, dict)
    found: list[dict[str, str]] = replayed.data["messages"]
    return found


def test_ids_and_headers_in_list_order(plugin: Plugin) -> None:
    first, second, *_ = messages(run(plugin))
    assert first == {
        "id": "18f0a1b2c3d4e5f1",
        "thread_id": "18f0a1b2c3d4e5f1",
        "from": "Ada Example \\<ada@example.org\\>",
        "subject": "Quarterly report draft",
        "date": "Mon, 02 Nov 2026 08:15:00 +0100",
    }
    assert second["subject"] == "Lunch on Friday?"  # header names are case-insensitive
    assert second["thread_id"] == "18f0a1b2c3d4e5f0"


def test_max_results_bounds_the_list_and_the_gets(google: FakeGoogle, plugin: Plugin) -> None:
    assert len(messages(run(plugin, max_results=2))) == 2
    listing = urlsplit(google.seen[1]["url"])
    assert parse_qs(listing.query)["maxResults"] == ["2"]
    assert google.routes().count("list") == 1
    assert len([r for r in google.routes() if r.startswith("get:")]) == 2


@pytest.mark.parametrize("bad", [0, 26, -1, 2.5, None, "many"])
def test_max_results_is_validated(plugin: Plugin, google: FakeGoogle, bad: object) -> None:
    replayed = run(plugin, max_results=bad)
    assert replayed.error is not None
    assert google.seen == []


def test_no_body_is_ever_requested(google: FakeGoogle, plugin: Plugin) -> None:
    messages(run(plugin))
    for seen in google.seen:
        if not seen["route"].startswith("get:"):
            continue
        query = parse_qs(urlsplit(seen["url"]).query)
        assert query["format"] == ["metadata"]
        assert query["metadataHeaders"] == ["From", "Subject", "Date"]
        assert query["fields"] == ["id,threadId,payload/headers"]


def test_hostile_headers_are_escaped_and_capped(plugin: Plugin) -> None:
    hostile = messages(run(plugin))[2]
    for value in hostile.values():
        assert "\n" not in value
        assert "\r" not in value
        assert "\x00" not in value
        assert "‮" not in value
    assert "📅" not in hostile["subject"]  # the Tasks due-date signifier is made inert
    assert "\\[\\[" in hostile["subject"]
    assert hostile["subject"].startswith("Pay now")
    assert len(hostile["date"]) <= gmail.MAX_DATE
    assert hostile["date"].endswith("…")


def test_long_headers_are_cut(google: FakeGoogle) -> None:
    body = {
        "id": "18f0a1b2c3d4e5f1",
        "payload": {"headers": [{"name": "Subject", "value": "x" * 5000}]},
    }
    google.script = {"get:18f0a1b2c3d4e5f1": [(200, json.dumps(body).encode())]}
    (message, *_) = messages(run(build(google, environ=ENVIRON), max_results=1))
    assert len(message["subject"]) <= gmail.MAX_SUBJECT


def test_a_message_without_the_headers_has_empty_ones(plugin: Plugin) -> None:
    bare = messages(run(plugin))[3]
    assert (bare["from"], bare["subject"], bare["date"]) == ("", "", "")


def test_a_deleted_message_is_skipped(google: FakeGoogle) -> None:
    google.script = {"get:18f0a1b2c3d4e5f1": [(404, recorded("not-found"))]}
    replayed = run(build(google, environ=ENVIRON), max_results=2)
    assert [m["id"] for m in messages(replayed)] == ["18f0a1b2c3d4e5f2"]
    assert isinstance(replayed.data, dict)
    assert replayed.data["skipped"] == 1


@pytest.mark.parametrize(
    "listed",
    [
        {"messages": [{"id": "../../etc/passwd"}, {"id": 7}, "x", {"threadId": "t"}]},
        {"messages": "nope"},
    ],
)
def test_untrusted_list_entries_are_skipped_or_refused(google: FakeGoogle, listed: Any) -> None:
    google.script = {"list": [(200, json.dumps(listed).encode())]}
    replayed = run(build(google, environ=ENVIRON))
    if isinstance(listed["messages"], list):
        assert messages(replayed) == []
        assert not [r for r in google.routes() if r.startswith("get:")]
    else:
        assert replayed.error == "bad_response"


def test_an_empty_mailbox(google: FakeGoogle) -> None:
    google.script = {"list": [(200, b"{}")]}
    assert messages(run(build(google, environ=ENVIRON))) == []


def test_a_message_that_is_not_the_one_asked_for_is_skipped(google: FakeGoogle) -> None:
    google.script = {"get:18f0a1b2c3d4e5f1": [(200, recorded("message-18f0a1b2c3d4e5f2"))]}
    found = messages(run(build(google, environ=ENVIRON), max_results=1))
    assert found == []


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (429, b"{}", "rate_limited"),
        (403, b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}', "rate_limited"),
        (403, b'{"error": {"errors": [{"reason": "insufficientPermissions"}]}}', "http_403"),
        (403, b'{"error": "x"}', "http_403"),
        (400, b"{}", "http_400"),
    ],
)
def test_api_failures_are_fixed_codes(
    google: FakeGoogle, status: int, body: bytes, reason: str
) -> None:
    google.script = {"list": [(status, body)]}
    assert run(build(google, environ=ENVIRON)).error == reason


def test_a_failing_get_fails_the_call(google: FakeGoogle) -> None:
    google.script = {"get:18f0a1b2c3d4e5f1": [(429, b"{}")]}
    assert run(build(google, environ=ENVIRON), max_results=2).error == "rate_limited"
