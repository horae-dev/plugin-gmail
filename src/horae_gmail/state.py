# SPDX-License-Identifier: Apache-2.0
"""Since-state: per account label, the Gmail `historyId` the last run reached.

A JSON file at a path the core approved for writing (`call.paths.allowed(path, write=True)`,
granted in `horae.lock`). It holds one decimal string per account, no mail content and no
secret. It is read at the start of a call and written once, at the end, by an atomic replace
(`os.replace` of a sibling file, mode 0600), so a call that raises leaves it as it was and the
next run reads the same mail again.
"""

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path

from horae_sdk import PluginError

VERSION = 1
MAX_BYTES = 64 * 1024
HISTORY_ID = re.compile(r"[0-9]{1,20}")


def load(path: str) -> dict[str, str]:
    """`{account: history_id}`; an absent file is an empty state, anything else unreadable
    raises (a corrupt state must not silently become a re-baseline)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        raise PluginError("state_unreadable: cannot read the state file") from None
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("too large")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != VERSION:
            raise ValueError("version")
        accounts = document["accounts"]
        result: dict[str, str] = {}
        for account, entry in accounts.items():
            history = entry["history_id"]
            if not isinstance(account, str) or not HISTORY_ID.fullmatch(history):
                raise ValueError("history id")
            result[account] = history
        return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise PluginError("state_invalid: the state file is not a valid gmail state") from None


def save(path: str, state: dict[str, str]) -> None:
    """Write `state` atomically (a temporary sibling, then `os.replace`)."""
    document = {
        "version": VERSION,
        "accounts": {account: {"history_id": state[account]} for account in sorted(state)},
    }
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(dir=Path(path).parent, prefix=".gmail-state-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        raise PluginError("state_unwritable: cannot write the state file") from None
