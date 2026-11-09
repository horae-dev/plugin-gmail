# SPDX-License-Identifier: Apache-2.0
"""The hosts approved in `horae.lock` (`Call.host_allowed`): an unapproved Google host is
refused by name without contacting anything; `network=None` is an older core, which sent no
list (the declared hosts apply)."""

from collections.abc import Sequence
from typing import Any

import anyio
import pytest
from horae_sdk.testing import Replayed, call

from conftest import ENVIRON, FakeGoogle
from horae_gmail import HOSTS, build


def run(google: FakeGoogle, network: Sequence[str] | None, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call(build(google, environ=ENVIRON), "gmail.recent", params, network=network)

    return anyio.run(go)


def test_the_manifest_declares_exactly_the_two_hosts() -> None:
    assert HOSTS == ("gmail.googleapis.com", "oauth2.googleapis.com")


def test_approved_hosts_are_contacted(google: FakeGoogle) -> None:
    assert run(google, list(HOSTS), max_results=1).error is None
    assert google.seen


@pytest.mark.parametrize(
    ("network", "host"),
    [
        ([], "oauth2.googleapis.com"),
        (["gmail.googleapis.com"], "oauth2.googleapis.com"),
        (["oauth2.googleapis.com"], "gmail.googleapis.com"),
    ],
)
def test_an_unapproved_host_is_refused_without_contact(
    google: FakeGoogle, network: list[str], host: str
) -> None:
    replayed = run(google, network)
    assert replayed.error == f"host_not_approved: {host}"
    assert google.seen == []


def test_an_older_core_without_a_list_keeps_the_declared_hosts(google: FakeGoogle) -> None:
    assert run(google, None, max_results=1).error is None
