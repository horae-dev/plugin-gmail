# SPDX-License-Identifier: Apache-2.0
"""Gmail labels by name: `users.labels.list`, read once per run, and the resolution of a label
id or name to an id.

`labels.list` is a GET that `gmail.readonly` and `gmail.metadata` both allow (Google's
reference for the method), so **no scope is added**. It is called at most once per
`gmail.triage` call, and only when a label has to be resolved: the system labels (`INBOX`,
`CATEGORY_PROMOTIONS`...) are known ids and never need it, so a configuration that names only
those makes no extra call. The list lives in memory for that call.

**Resolution of a reference** (`Labels.resolve`), in this order, first step that finds
something wins:

1. a label **id** (`INBOX`, `Label_12`): exact;
2. a label **name** (`Receipts`, `Clients/Acme`), exact case;
3. a label name, ignoring case.

Zero matches at the step that finds something is the answer; two or more are never guessed:
`LabelError("label_ambiguous")` names the ids so the configuration can use one. No match is
`LabelError("label_not_found")`. A label's name is the user's own text, so it is never put in
an error or an output: only the reference the configuration gave, and ids.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from horae_gmail.gmail import Gmail, failure
from horae_gmail.oauth import GoogleError

LABELS_URL_PATH = "/labels"
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_NAME = 225
"""Gmail's own limit for a label name."""
MAX_LABELS = 5000
"""More than this and the answer is refused, not read."""
SYSTEM_IDS = frozenset(
    {
        "INBOX",
        "SENT",
        "DRAFT",
        "SPAM",
        "TRASH",
        "UNREAD",
        "STARRED",
        "IMPORTANT",
        "CHAT",
        "CATEGORY_PERSONAL",
        "CATEGORY_SOCIAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
)
"""Gmail's system label ids: fixed, so they resolve without a call."""
GLOB_CHARS = frozenset("*?[")


class LabelError(Exception):
    """A label reference that cannot be resolved; `code` is `label_not_found` or
    `label_ambiguous`, `detail` is safe to show (the reference and ids only)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def is_literal(pattern: str) -> bool:
    """Whether a `fnmatch` pattern has no wildcard, so it names one label."""
    return not GLOB_CHARS & set(pattern)


def list_url(api_url: str) -> str:
    base = api_url.removesuffix("/messages")
    return f"{base}{LABELS_URL_PATH}?{urlencode({'fields': 'labels(id,name)'})}"


@dataclass(frozen=True)
class Labels:
    """The mailbox's labels: `names` maps an id to its name. Empty when never read."""

    names: Mapping[str, str]

    def resolve(self, reference: str) -> str:
        """The id `reference` (an id or a name) stands for, or a `LabelError`."""
        if reference in self.names:
            return reference
        for fold in (False, True):
            wanted = reference.casefold() if fold else reference
            found = sorted(
                label_id
                for label_id, name in self.names.items()
                if (name.casefold() if fold else name) == wanted
            )
            if len(found) == 1:
                return found[0]
            if found:
                raise LabelError(
                    "label_ambiguous",
                    f"{reference!r} names {len(found)} labels ({', '.join(found)}); "
                    "use the label id",
                )
        raise LabelError("label_not_found", f"no label has the id or name {reference!r}")

    def name_of(self, label_id: str) -> str | None:
        return self.names.get(label_id)


async def fetch(gmail: Gmail, api_url: str) -> Labels:
    """The mailbox's labels (`GoogleError` when the call fails or the answer is not a list)."""
    status, data = await gmail.get(list_url(api_url))
    if status != 200:
        raise GoogleError(failure(status, data))
    listed = data.get("labels", [])
    if not isinstance(listed, list) or len(listed) > MAX_LABELS:
        raise GoogleError("bad_response")
    names: dict[str, str] = {}
    for item in listed:
        if not isinstance(item, dict):
            raise GoogleError("bad_response")
        label_id, name = item.get("id"), item.get("name")
        if (
            isinstance(label_id, str)
            and ID.fullmatch(label_id)
            and isinstance(name, str)
            and 0 < len(name) <= MAX_NAME
        ):
            names[label_id] = name
    return Labels(names)


__all__ = ["SYSTEM_IDS", "LabelError", "Labels", "fetch", "is_literal"]
