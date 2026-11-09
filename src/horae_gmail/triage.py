# SPDX-License-Identifier: Apache-2.0
"""Gmail API v1 for the triage: the mail that arrived since the last `historyId`.

`users.history.list` (`historyTypes=messageAdded`, one label, `INBOX` by default) gives the ids
of the messages added after the state's `historyId`; `messages.get` with `format=full` reads
each. Only GETs, only the two read-only scopes: nothing is marked read, moved or deleted.

The first run has no state: it asks `users.getProfile` for the current `historyId` and handles
nothing (a flood of old mail must not become tasks). When Google no longer has the state's
`historyId` (404: history is kept for about a week) the state is **reset** the same way, and
the output says so (`reset`).
"""

import asyncio
import time
from urllib.parse import quote, urlencode

from horae_gmail import mail
from horae_gmail.fetch import FetchError
from horae_gmail.gmail import API_URL, MESSAGE_ID, Gmail, failure
from horae_gmail.oauth import GoogleError
from horae_gmail.state import HISTORY_ID

USER_URL = API_URL.removesuffix("/messages")
PAGE_SIZE = 100
MAX_PAGES = 10
CONCURRENCY = 5
BUDGET_S = 20.0
"""Wall-clock time to read the messages; past it the call fails and the state does not move."""


def profile_url() -> str:
    return f"{USER_URL}/profile?{urlencode({'fields': 'historyId'})}"


def history_url(start: str, label: str, token: str | None) -> str:
    params = {
        "startHistoryId": start,
        "historyTypes": "messageAdded",
        "labelId": label,
        "maxResults": PAGE_SIZE,
        "fields": "history(id,messagesAdded/message/id),historyId,nextPageToken",
    }
    if token:
        params["pageToken"] = token
    return f"{USER_URL}/history?{urlencode(params)}"


def full_url(message_id: str) -> str:
    return f"{API_URL}/{quote(message_id, safe='')}?{urlencode({'format': 'full'})}"


class HistoryExpired(Exception):
    """Google no longer has the state's `historyId`."""


def _history_id(value: object) -> str | None:
    return value if isinstance(value, str) and HISTORY_ID.fullmatch(value) else None


async def current(gmail: Gmail) -> str:
    """The mailbox's current `historyId`."""
    status, data = await gmail.get(profile_url())
    if status != 200:
        raise GoogleError(failure(status, data))
    found = _history_id(data.get("historyId"))
    if found is None:
        raise GoogleError("bad_response")
    return found


async def new_ids(gmail: Gmail, start: str, label: str, limit: int) -> tuple[list[str], str, bool]:
    """`(ids, history_id, more)`: the ids of the messages added after `start`, oldest first and
    at most about `limit` (whole history records are taken), the `historyId` the next run must
    start from, and whether more mail is waiting. Raises `HistoryExpired` on a 404."""
    ids: list[str] = []
    reached = start
    token: str | None = None
    for _ in range(MAX_PAGES):
        status, data = await gmail.get(history_url(start, label, token))
        if status == 404:
            raise HistoryExpired
        if status != 200:
            raise GoogleError(failure(status, data))
        records = data.get("history", [])
        if not isinstance(records, list):
            raise GoogleError("bad_response")
        for record in records:
            found = _record(record)
            if found is None:
                raise GoogleError("bad_response")
            record_id, added = found
            fresh = [m for m in dict.fromkeys(added) if m not in ids]
            if ids and len(ids) + len(fresh) > limit:
                return ids, reached, True
            ids.extend(fresh)
            reached = record_id
        latest = _history_id(data.get("historyId"))
        token = data.get("nextPageToken")
        if not isinstance(token, str) or not token:
            if latest is None:
                raise GoogleError("bad_response")
            return ids, latest, False
    return ids, reached, True  # too many pages for one call: the next run continues


def _record(record: object) -> tuple[str, list[str]] | None:
    if not isinstance(record, dict):
        return None
    record_id = _history_id(record.get("id"))
    added = record.get("messagesAdded", [])
    if record_id is None or not isinstance(added, list):
        return None
    ids: list[str] = []
    for entry in added:
        message = entry.get("message") if isinstance(entry, dict) else None
        found = message.get("id") if isinstance(message, dict) else None
        if isinstance(found, str) and MESSAGE_ID.fullmatch(found):
            ids.append(found)
    return record_id, ids


async def fetch(gmail: Gmail, ids: list[str], body_bytes: int) -> tuple[list[mail.Message], int]:
    """The messages (in `ids` order) and how many could not be read: deleted since (404),
    over the size cap, or not the message asked for. Another failure raises, and so does
    running out of time, so the state does not move past mail that was not read."""
    deadline = time.monotonic() + BUDGET_S
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(message_id: str) -> mail.Message | None:
        async with gate:
            if time.monotonic() > deadline:
                raise FetchError("timeout")
            try:
                status, body = await gmail.get(full_url(message_id))
            except FetchError as error:
                if error.reason == "too_large":
                    return None
                raise
        if status == 404:
            return None
        if status != 200:
            raise GoogleError(failure(status, body))
        return mail.parse(body, message_id, body_bytes=body_bytes)

    found = await asyncio.gather(*(one(message_id) for message_id in ids))
    messages = [message for message in found if message is not None]
    return messages, len(ids) - len(messages)


__all__ = ["HistoryExpired", "current", "fetch", "new_ids"]
