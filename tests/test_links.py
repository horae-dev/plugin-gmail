# SPDX-License-Identifier: Apache-2.0
"""Multi-account task links: `account_index` picks `/mail/u/<n>/`, and the core's
`link_prefix` check still routes them. Synthetic data only."""

import asyncio
import datetime
from pathlib import Path
from typing import Any

import pytest
from horae.engine import execute, make_context
from horae.engine.ports import ToolResult
from horae.engine.testing import FakeLlm, FixturePlugins
from horae.skills import DirectorySkills

from horae_gmail import plugin
from skill import ROOT, manifest, pipeline
from test_triage import BILL, run, setup

START = datetime.datetime(2026, 11, 9, 5, 30, tzinfo=datetime.UTC)
SINK = {
    "name": "obsidian",
    "version": "0.1.0",
    "horae": ">=0",
    "provides": {"sinks": ["obsidian.write_task"]},
    "permissions": {"intents": ["task.create", "task.update_status"]},
}


def task_link(tmp_path: Path, **params: Any) -> tuple[list[dict[str, Any]], Any]:
    google, path = setup(tmp_path, BILL)
    replayed = run(google, path, **params)
    assert replayed.error is None, replayed.error
    return replayed.intents, replayed.data


def test_the_default_account_is_zero(tmp_path: Path) -> None:
    (task,), _ = task_link(tmp_path)
    assert task["link"] == "https://mail.google.com/mail/u/0/#all/" + BILL["id"]


@pytest.mark.parametrize("index", [0, 1, 7, 99])
def test_account_index_chooses_the_gmail_account(tmp_path: Path, index: int) -> None:
    (task,), _ = task_link(tmp_path, account_index=index)
    assert task["link"] == f"https://mail.google.com/mail/u/{index}/#all/{BILL['id']}"


def test_the_account_index_does_not_change_the_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (first,), _ = task_link(tmp_path / "a", account_index=0)
    (second,), _ = task_link(tmp_path / "b", account_index=3)
    assert first["fingerprint"] == second["fingerprint"] == "gmail:" + BILL["id"]


def test_the_exact_prefix_of_an_account() -> None:
    assert plugin.link_prefix_for(2) == "https://mail.google.com/mail/u/2/"
    assert plugin.link_prefix_for(2).startswith(plugin.LINK_PREFIX)


def _routed(link_prefix: Any, tmp_path: Path, index: int) -> Any:
    """Run the example pipeline in the core's engine with the real plugin's intents and
    `link_prefix` as the tasks entry's; the core's own routing decides."""
    intents, _ = task_link(tmp_path, account_index=index)
    spec = pipeline().model_dump(mode="json", by_alias=True, exclude_none=True)
    spec["steps"][-1]["act"][0]["obsidian.write_task"]["link_prefix"] = link_prefix
    plugins = FixturePlugins()
    plugins.add(
        manifest(),
        {"gmail.triage": lambda params, inputs: ToolResult({"needs_interpretation": []}, intents)},
        commit="a" * 40,
    )
    plugins.add(SINK, {})
    return asyncio.run(
        execute(
            type(pipeline()).model_validate(spec),
            run_id="20261109T053000Z-triage-mail-01",
            context=make_context(START, "Europe/Paris"),
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=FakeLlm([]),
            skills=DirectorySkills({"gmail": ROOT}),
        )
    )


def test_the_core_routes_each_accounts_link_under_its_exact_prefix(tmp_path: Path) -> None:
    record = _routed(plugin.link_prefix_for(1), tmp_path, 1)
    assert [i.intent.type for i in record.intents] == ["task.create"]


def test_the_core_routes_every_account_under_the_shared_prefix(tmp_path: Path) -> None:
    record = _routed(plugin.LINK_PREFIX, tmp_path, 4)
    assert [i.intent.type for i in record.intents] == ["task.create"]


def test_the_old_account_zero_prefix_does_not_cover_another_account(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    record = _routed(plugin.link_prefix_for(0), tmp_path, 1)
    assert record.intents == []  # the core skips a task whose link is outside the prefix
    assert "task link not under a trusted link_prefix" in caplog.text
