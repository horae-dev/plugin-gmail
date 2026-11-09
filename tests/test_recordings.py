# SPDX-License-Identifier: Apache-2.0
"""Replay every recorded call (`tests/recordings/*.json`) through MCP, as the core's host
makes it. `HORAE_SDK_RECORD=1 uv run pytest` rewrites the expectations: review the diff."""

from pathlib import Path

from horae_sdk import Plugin
from horae_sdk.testing import replay_all

from conftest import no_leak

RECORDINGS = Path(__file__).parent / "recordings"


def test_recordings(plugin: Plugin) -> None:
    replayed = replay_all(plugin, RECORDINGS)
    assert len(replayed) >= 2, "the recordings directory looks empty"


def test_no_recording_holds_a_credential() -> None:
    for path in RECORDINGS.glob("*.json"):
        no_leak(path.read_text("utf-8"))
