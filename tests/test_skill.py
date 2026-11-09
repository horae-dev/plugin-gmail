# SPDX-License-Identifier: Apache-2.0
"""The skill `gmail/triage-mail` and its golden cases, through the core's own code.

`horae eval` runs offline: each case goes through the real Anthropic render, the production
output validator, the intent checks and the action guard, and the recorded answer replaces the
model (the public replay, `horae.llm.replay`, schemas decision 64). The recorded answers are
**hand-written** synthetic answers, never a model's: their wording is for the maintainer to
judge, which is why the cases assert properties (counts, references, words), not exact text."""

import json
from typing import Any

from horae.cli import app
from typer.testing import CliRunner

from skill import ROOT, SKILL, cases, manifest, message_ids, referenced_ids, skill

runner = CliRunner()

MAIL_TYPES = {
    "invoice-due",
    "meeting-request",
    "school-notice",
    "newsletter",
    "shipping-notice",
    "personal-request",
    "phishing-urgent",
    "low-confidence",
    "mixed-batch",
    "empty-batch",
    "injection-ignored",
}
OBEYED = {"injected-link": "violation"}
"""A model that obeyed an injection, and what the action guard does with it."""


def _eval() -> tuple[int, Any]:
    result = runner.invoke(app, ["eval", SKILL, "--plugin-dir", str(ROOT), "--json"])
    return result.exit_code, json.loads(result.stdout)


def test_every_golden_case_passes_offline_and_none_is_stale() -> None:
    code, report = _eval()
    failed = [(c["case"], c["status"], c["error"], c["checks"]) for c in report["cases"]]
    assert code == 0, failed
    summary = report["summary"]
    assert report["mode"] == "offline"
    assert summary["failed"] == 0
    assert summary["errors"] == 0
    # A stale recording: run scripts/sync_recordings.py (hand-written answers only).
    assert summary["stale"] == 0, [(c["case"], c["stale"]) for c in report["cases"]]
    assert summary["passed"] == summary["cases"] == len(MAIL_TYPES) + len(OBEYED)


def test_the_cases_are_the_common_mail_types_and_the_obeyed_injection() -> None:
    assert set(cases()) == MAIL_TYPES | set(OBEYED)


def test_every_case_is_recorded_for_the_default_backend_by_hand() -> None:
    for name, loaded in cases().items():
        assert loaded.recorded["anthropic"].model == "claude-fixture-1", name


def test_the_guard_on_the_obeyed_injection() -> None:
    _, report = _eval()
    guard = {c["case"]: c["guard"] for c in report["cases"]}
    for name, expected in OBEYED.items():
        assert cases()[name].case.expect.guard == expected, name
        assert guard[name] == expected, name


def test_the_skill_is_links_none_and_proposes_only_task_create() -> None:
    loaded = skill()
    assert loaded.links == "none"
    assert dict(loaded.intent_limits) == {"task.create": 25}
    assert manifest().permissions.intents == ["task.create"]
    assert manifest().provides.skills == [SKILL]


def test_the_view_shows_the_undecided_mail_and_nothing_a_rule_decided() -> None:
    case_input = cases()["mixed-batch"].case.input
    case_input = json.loads(json.dumps(case_input))
    data = case_input[0]["data"]
    data["decisions"].append(
        {"id": "18f0c0000000bfff", "action": "task", "decided": True, "rule": "bills"}
    )
    viewed = skill().view(case_input)
    assert [m["id"] for m in viewed[0]["data"]["needs_interpretation"]] == [
        "18f0b0000000a001",
        "18f0b0000000a004",
        "18f0b0000000a007",
        "18f0b0000000a006",
    ]
    text = json.dumps(viewed)
    assert "bfff" not in text
    assert "decisions" not in text
    assert "state_path" not in text
    assert "thread_id" not in text


def test_when_rules_decide_everything_the_view_holds_no_mail() -> None:
    viewed = skill().view(cases()["empty-batch"].case.input)
    assert viewed[0]["data"]["needs_interpretation"] == []


def test_each_golden_answer_cites_only_messages_that_were_in_its_input() -> None:
    """The reference check: a model that cites an id it was not shown (a hallucinated
    fingerprint) is caught here for the golden answers, and by the same helper in the test
    below. The core's guard checks a `task.update_status` fingerprint against the viewed
    input; it has no such check for a `task.create` (see the README, "Follow-ups")."""
    for name, loaded in cases().items():
        shown = message_ids(loaded.case.input)
        output = loaded.recorded["anthropic"].output
        assert referenced_ids(output) <= shown, name
        assert {t["message_id"] for t in output["triaged"]} == shown, name


def test_the_reference_check_catches_an_invented_id() -> None:
    loaded = cases()["invoice-due"]
    answer = json.loads(json.dumps(loaded.recorded["anthropic"].output))
    answer["intents"][0]["fingerprint"] = "gmail:18f0b0000000ffff"
    assert not referenced_ids(answer) <= message_ids(loaded.case.input)


def test_every_golden_input_has_the_shape_gmail_triage_returns() -> None:
    """The cases' `data` has the keys the plugin's output has, and the caps it applies."""
    keys = {
        "account", "baselined", "reset", "decisions", "counts",
        "needs_interpretation", "truncated", "skipped", "state_updated",
    }  # fmt: skip
    for name, loaded in cases().items():
        (entry,) = loaded.case.input
        assert entry["source"] == "gmail.triage", name
        assert set(entry["data"]) == keys, name
        for message in entry["data"]["needs_interpretation"]:
            assert set(message) == {"id", "from", "subject", "date", "snippet"}, name
            assert len(message["snippet"]) <= 1000, name
            assert "\n" not in message["snippet"], name
            assert "<" not in message["snippet"].replace("\\<", ""), name


def test_the_example_pipeline_loads_and_routes_the_gmail_link() -> None:
    from skill import pipeline

    loaded = pipeline()
    assert loaded.name == "triage-mail"
    text = json.dumps(loaded.model_dump(mode="json"))
    assert "gmail.triage" in text
    assert "gmail/triage-mail" in text
    assert "https://mail.google.com/mail/u/0/" in text
