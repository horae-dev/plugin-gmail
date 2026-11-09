# SPDX-License-Identifier: Apache-2.0
"""Gmail API v1: the most recent messages' ids and three headers, and nothing else.

`messages.list` gives the ids; `messages.get` with `format=metadata` and
`metadataHeaders=From,Subject,Date` gives the headers. **No message body is requested or
read**, so the source works with `gmail.metadata` as well as `gmail.readonly`; it proves the
token works end to end, and story #3 builds the triage on it.

A 401 makes one fresh token exchange and one retry (`Gmail.get`). Every failure is a
`GoogleError` or `FetchError` with a fixed `reason`, never Google's message.

**The answers are untrusted**: a header is written by whoever sent the mail. Each is made one
line of plain text by `escape_title` (control and format characters removed, Obsidian
Tasks signifiers and Markdown made inert) and capped (`MAX_FROM`, `MAX_SUBJECT`, `MAX_DATE`).
A message that cannot be read is counted in `skipped`.
"""

import asyncio
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

from horae_sdk.tasks import escape_title

from horae_gmail.fetch import Api
from horae_gmail.oauth import AccessTokens, GoogleError, parse_json

API_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
HEADERS = ("From", "Subject", "Date")
DEFAULT_MESSAGES = 10
MAX_MESSAGES = 25
CONCURRENCY = 5
BUDGET_S = 20.0
"""Wall-clock time for the whole read; the messages not reached by then are skipped."""
MAX_FROM = 200
MAX_SUBJECT = 200
MAX_DATE = 64
MESSAGE_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
_RATE_LIMITED = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


def list_url(count: int) -> str:
    return f"{API_URL}?{urlencode({'maxResults': count, 'fields': 'messages(id,threadId)'})}"


def get_url(message_id: str) -> str:
    params = [
        ("format", "metadata"),
        *(("metadataHeaders", name) for name in HEADERS),
        ("fields", "id,threadId,payload/headers"),
    ]
    return f"{API_URL}/{quote(message_id, safe='')}?{urlencode(params)}"


class Gmail:
    """The Gmail calls of one plugin call, with its access token."""

    def __init__(self, api: Api, tokens: AccessTokens) -> None:
        self._api = api
        self._tokens = tokens

    async def get(self, url: str) -> tuple[int, dict[str, Any]]:
        """The status and JSON of a GET; after a 401, one fresh token and one retry."""
        token = await self._tokens.get()
        for attempt in (1, 2):
            status, answer = await self._api.api(
                "GET", url, headers={"Authorization": f"Bearer {token}"}
            )
            if status == 401 and attempt == 1:
                token = await self._tokens.get(refused=token)
                continue
            return status, parse_json(answer)
        raise GoogleError("auth_failed")  # pragma: no cover (the loop always returns)


async def recent(api: Api, tokens: AccessTokens, count: int) -> tuple[list[dict[str, str]], int]:
    """The `count` most recent messages' headers and how many could not be read
    (`GoogleError` or `FetchError` when the list itself fails)."""
    gmail = Gmail(api, tokens)
    deadline = time.monotonic() + BUDGET_S
    status, data = await gmail.get(list_url(count))
    if status != 200:
        raise GoogleError(failure(status, data))
    listed = data.get("messages", [])
    if not isinstance(listed, list):
        raise GoogleError("bad_response")
    ids = [
        item["id"]
        for item in listed[:count]
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and MESSAGE_ID.fullmatch(item["id"])
    ]
    skipped = min(len(listed), count) - len(ids)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(message_id: str) -> dict[str, str] | None:
        async with gate:
            if time.monotonic() > deadline:
                return None
            status, body = await gmail.get(get_url(message_id))
        if status == 404:  # deleted since the list
            return None
        if status != 200:
            raise GoogleError(failure(status, body))
        return to_message(body, message_id)

    found = await asyncio.gather(*(one(message_id) for message_id in ids))
    messages = [message for message in found if message is not None]
    return messages, skipped + len(ids) - len(messages)


def to_message(body: dict[str, Any], message_id: str) -> dict[str, str] | None:
    """The common shape of a `messages.get` answer; `None` when it is not the message asked
    for."""
    if body.get("id") != message_id:
        return None
    payload = body.get("payload")
    headers = payload.get("headers") if isinstance(payload, dict) else None
    wanted: dict[str, str] = {}
    for header in headers if isinstance(headers, list) else []:
        if not isinstance(header, dict):
            continue
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            wanted.setdefault(name.lower(), value)
    thread = body.get("threadId")
    return {
        "id": message_id,
        "thread_id": thread if isinstance(thread, str) and MESSAGE_ID.fullmatch(thread) else "",
        "from": clean(wanted.get("from"), MAX_FROM),
        "subject": clean(wanted.get("subject"), MAX_SUBJECT),
        "date": clean(wanted.get("date"), MAX_DATE),
    }


def clean(raw: str | None, max_length: int) -> str:
    """An untrusted header as one inert line of at most `max_length` characters; `""` when
    the header is missing or has nothing visible."""
    if raw is None or not any(char.isprintable() and not char.isspace() for char in raw):
        return ""
    return escape_title(raw, max_length=max_length)


def failure(status: int, data: dict[str, Any]) -> str:
    """The fixed code of a failed API answer."""
    if status == 401:
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    if status == 403 and reasons(data) & _RATE_LIMITED:
        return "rate_limited"
    return f"http_{status}"


def reasons(data: dict[str, Any]) -> set[str]:
    error = data.get("error")
    errors = error.get("errors") if isinstance(error, dict) else None
    if not isinstance(errors, list):
        return set()
    return {e["reason"] for e in errors if isinstance(e, dict) and isinstance(e.get("reason"), str)}
