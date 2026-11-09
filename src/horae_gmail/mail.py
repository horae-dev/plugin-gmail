# SPDX-License-Identifier: Apache-2.0
"""A Gmail message as the triage reads it: untrusted headers and the plain-text body.

`messages.get` with `format=full` is the answer parsed here. **Only `text/plain` is read**:
HTML parts are ignored (never parsed, never returned), and so is every part that is an
attachment (it has a file name or an `attachmentId`; the attachment itself is never fetched).
The text is cut to a byte budget before it is decoded, quoted reply lines are dropped, and the
snippet that can reach the LLM step is made one inert line by `escape_title` and capped.
Nothing in a message is ever fetched or followed: a link in the text is neutralised, never
opened, and never becomes a task link.
"""

import base64
import binascii
import codecs
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parseaddr
from typing import Any

from horae_sdk.tasks import escape_title

from horae_gmail.gmail import MAX_DATE, MAX_FROM, MAX_SUBJECT, MESSAGE_ID, clean

MAX_PARTS = 50
MAX_DEPTH = 6
MATCH_CHARS = 300
"""Characters of a header the rule patterns are tried against."""
LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CHARSET = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9_.:+-]{1,40})", re.IGNORECASE)


@dataclass(frozen=True)
class Message:
    id: str
    thread_id: str
    sender: str
    """The bare address, lower case, for the rules."""
    subject: str
    """The raw subject (cut), for the rules and the task title."""
    list_id: str
    labels: tuple[str, ...]
    day: date | None
    text: str
    """The start of the `text/plain` body, decoded, not yet escaped (never returned as is)."""
    from_line: str
    """The `From` header as one inert line (name and address), for the LLM step."""
    subject_line: str
    date_line: str


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    headers = payload.get("headers")
    for header in headers if isinstance(headers, list) else []:
        if not isinstance(header, dict):
            continue
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            found.setdefault(name.lower(), value)
    return found


def _list_id(value: str) -> str:
    """The `List-Id` identifier (`The Digest <news.example.com>` is `news.example.com`), lower
    case, cut."""
    start, end = value.find("<"), value.rfind(">")
    inner = value[start + 1 : end] if 0 <= start < end else value
    return inner.strip()[:MATCH_CHARS].lower()


def _decode(data: str, budget: int, charset: str) -> str:
    """The start of base64url `data` as text, at most about `budget` bytes."""
    head = data[: (budget * 4 + 2) // 3].rstrip("=")
    if len(head) < len(data.rstrip("=")):  # cut: whole groups of four characters only
        head = head[: len(head) - len(head) % 4]
    head += "=" * (-len(head) % 4)  # Gmail may or may not pad
    try:
        raw = base64.urlsafe_b64decode(head)
    except (binascii.Error, ValueError):
        return ""
    try:
        encoding = codecs.lookup(charset).name
    except LookupError:
        encoding = "utf-8"
    return raw[:budget].decode(encoding, errors="replace")


def _plain_text(payload: dict[str, Any], budget: int) -> str:
    """The first inline `text/plain` part of the message (breadth first, bounded)."""
    queue: list[tuple[dict[str, Any], int]] = [(payload, 0)]
    seen = 0
    while queue and seen < MAX_PARTS:
        part, depth = queue.pop(0)
        seen += 1
        body = part.get("body")
        body = body if isinstance(body, dict) else {}
        inline = not part.get("filename") and "attachmentId" not in body
        mime = part.get("mimeType")
        if inline and isinstance(mime, str) and mime.lower() == "text/plain":
            data = body.get("data")
            if isinstance(data, str) and data:
                match = _CHARSET.search(_headers(part).get("content-type", ""))
                return _decode(data, budget, match.group(1) if match else "utf-8")
        children = part.get("parts")
        if depth < MAX_DEPTH and isinstance(children, list):
            queue.extend((child, depth + 1) for child in children if isinstance(child, dict))
    return ""


def _day(body: dict[str, Any]) -> date | None:
    millis = body.get("internalDate")
    try:
        return datetime.fromtimestamp(int(millis) / 1000, UTC).date()  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse(body: dict[str, Any], message_id: str, *, body_bytes: int) -> Message | None:
    """The message, or `None` when the answer is not the message asked for."""
    if body.get("id") != message_id:
        return None
    payload = body.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    wanted = _headers(payload)
    thread = body.get("threadId")
    raw_labels = body.get("labelIds")
    labels = tuple(
        label
        for label in (raw_labels if isinstance(raw_labels, list) else [])
        if isinstance(label, str) and LABEL.fullmatch(label)
    )
    sender = parseaddr(wanted.get("from", "")[:MATCH_CHARS])[1].lower()
    return Message(
        id=message_id,
        thread_id=thread if isinstance(thread, str) and MESSAGE_ID.fullmatch(thread) else "",
        sender=sender,
        subject=wanted.get("subject", "")[:MATCH_CHARS],
        list_id=_list_id(wanted.get("list-id", "")),
        labels=labels[:50],
        day=_day(body),
        text=_plain_text(payload, body_bytes),
        from_line=clean(wanted.get("from"), MAX_FROM),
        subject_line=clean(wanted.get("subject"), MAX_SUBJECT),
        date_line=clean(wanted.get("date"), MAX_DATE),
    )


def snippet(message: Message, limit: int) -> str:
    """The body for the LLM step: quoted lines dropped, one inert line of at most `limit`
    characters (`escape_title`), `""` when there is no plain text."""
    lines = [line.strip() for line in message.text.splitlines()]
    kept = " ".join(line for line in lines if line and not line.startswith(">"))
    if not any(char.isprintable() and not char.isspace() for char in kept):
        return ""
    return escape_title(kept, max_length=limit)
