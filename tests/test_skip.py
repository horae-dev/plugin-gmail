# SPDX-License-Identifier: Apache-2.0
"""The example pipeline's `skip_when_empty` and the skill's `task_fingerprint`, run by the core.

The golden cases (`horae eval`) run the skill on one input; they cannot say that the step is
skipped. This runs the example pipeline itself through the core's engine (`horae.engine`) with
a fake model that would fail the test if it were called: nothing is sent anywhere."""

import asyncio
import datetime
import json
from typing import Any

from horae.contracts import RunRecord
from horae.engine import execute, make_context
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response
from horae.skills import DirectorySkills

from skill import ROOT, SKILL, cases, manifest, pipeline

START = datetime.datetime(2026, 11, 9, 5, 30, tzinfo=datetime.UTC)
SINK = {
    "name": "obsidian",
    "version": "0.1.0",
    "horae": ">=0",
    "provides": {"sinks": ["obsidian.write_task"]},
    "permissions": {"intents": ["task.create", "task.update_status"]},
}


def _run(case: str, llm: FakeLlm) -> RunRecord:
    data = json.loads(json.dumps(cases()[case].case.input))
    plugins = FixturePlugins()
    plugins.add(
        manifest(), {"gmail.triage": lambda params, inputs: data[0]["data"]}, commit="a" * 40
    )
    plugins.add(SINK, {"obsidian.write_task": lambda params, inputs: {}})
    return asyncio.run(
        execute(
            pipeline(),
            run_id="20261109T053000Z-triage-mail-01",
            context=make_context(START, "Europe/Paris"),
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            skills=DirectorySkills({"gmail": ROOT}),
        )
    )


def test_the_pipeline_skips_the_model_when_the_rules_decided_everything() -> None:
    llm = FakeLlm([])
    record = _run("empty-batch", llm)
    assert llm.requests == []
    assert record.llm == []
    assert record.cost.usd == 0
    step = next(s for s in record.steps if s.output == {"skipped": "empty_input", "skill": SKILL})
    assert step.status == "skipped"
    assert (record.status, record.intents) == ("noop", [])


def test_the_pipeline_calls_the_model_when_some_mail_needs_interpretation() -> None:
    answer = cases()["invoice-due"].recorded["anthropic"].output
    llm = FakeLlm(
        [
            llm_response(
                {k: v for k, v in answer.items() if k != "intents"}, intents=answer["intents"]
            )
        ]
    )
    record = _run("invoice-due", llm)
    assert len(llm.requests) == 1
    assert len(record.llm) == 1
    assert record.status == "ok"


def test_the_pipeline_refuses_a_fingerprint_that_is_not_a_viewed_mail() -> None:
    answer: Any = cases()["invented-id"].recorded["anthropic"].output
    llm = FakeLlm(
        [
            llm_response(
                {k: v for k, v in answer.items() if k != "intents"}, intents=answer["intents"]
            )
        ]
    )
    record = _run("invented-id", llm)
    assert len(llm.requests) == 1
    assert record.intents == []
    assert any(s.status == "degraded" and s.error for s in record.steps)
