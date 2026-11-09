# SPDX-License-Identifier: Apache-2.0
"""No client secret, refresh token or access token reaches the output, an error, a log record
or the manifest, whatever Google answers (even when it echoes them)."""

import logging
from pathlib import Path
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed, call

from conftest import ACCESS, APPROVED, CLIENT_SECRET, ENVIRON, REFRESH, FakeGoogle, dumps, no_leak
from horae_gmail import build


def run(plugin: Plugin, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call(plugin, "gmail.recent", params, network=APPROVED)

    return anyio.run(go)


ECHO = f'{{"echo": "Bearer {ACCESS} {CLIENT_SECRET} {REFRESH}"}}'.encode()


@pytest.mark.parametrize(
    "script",
    [
        {},
        {"token": [(400, ECHO)]},
        {"token": [(200, f'{{"access_token": "{ACCESS}", "scope": "{REFRESH}"}}'.encode())]},
        {"token": [(429, ECHO)]},
        {"list": [(401, ECHO)] * 3},
        {"list": [(403, ECHO)]},
        {"list": [(200, ECHO)]},
        {"get:18f0a1b2c3d4e5f1": [(500, ECHO)]},
        {"get:18f0a1b2c3d4e5f1": [(200, f'{{"id": "{REFRESH}"}}'.encode())]},
    ],
)
def test_tokens_never_reach_the_output_errors_or_logs(
    caplog: pytest.LogCaptureFixture, script: dict[str, list[tuple[int, bytes]]]
) -> None:
    google = FakeGoogle(script)
    with caplog.at_level(logging.DEBUG):
        replayed = run(build(google, environ=ENVIRON), max_results=2)
    no_leak(dumps(replayed.data), replayed.error or "", caplog.text, repr(replayed))


def test_an_unexpected_failure_logs_the_type_only(caplog: pytest.LogCaptureFixture) -> None:
    class Boom(FakeGoogle):
        async def api(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
            raise RuntimeError(f"boom {ACCESS} {REFRESH}")

    with caplog.at_level(logging.DEBUG):
        replayed = run(build(Boom(), environ=ENVIRON))
    assert replayed.error == "internal_error"
    no_leak(replayed.error, caplog.text)
    assert "RuntimeError" in caplog.text


def test_the_access_token_is_not_kept_after_the_call(google: FakeGoogle) -> None:
    plugin = build(google, environ=ENVIRON)
    run(plugin, max_results=1)
    run(plugin, max_results=1)
    assert google.routes().count("token") == 2  # a new exchange per call: nothing is cached


def test_nothing_is_written_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(build(FakeGoogle(), environ=ENVIRON))
    assert list(tmp_path.iterdir()) == []


def test_the_manifest_holds_secret_names_not_values() -> None:
    no_leak((Path(__file__).parent.parent / "plugin.yaml").read_text("utf-8"))
