# SPDX-License-Identifier: Apache-2.0
"""The rules and the state file, without the network."""

from datetime import date
from pathlib import Path

import pytest
from horae_sdk import PluginError

from horae_gmail import rules, state
from horae_gmail.mail import Message


def message(**fields: object) -> Message:
    base: dict[str, object] = {
        "id": "18f0c0000000c001",
        "thread_id": "",
        "sender": "ada@example.org",
        "subject": "Hello",
        "list_id": "",
        "labels": ("INBOX",),
        "day": date(2026, 11, 3),
        "text": "",
        "from_line": "",
        "subject_line": "Hello",
        "date_line": "",
    }
    return Message(**{**base, **fields})  # type: ignore[arg-type]


def test_patterns_are_case_insensitive_globs_on_each_field() -> None:
    parsed = rules.parse_rules(
        [{"name": "r", "action": "task", "from": ["*@EXAMPLE.org"], "subject": ["hel?o"]}]
    )
    assert rules.decide(parsed, message()).action == "task"
    assert rules.decide(parsed, message(subject="Other")).action == "interpret"
    assert rules.decide(parsed, message(sender="x@example.com")).rule is None


def test_a_rule_needs_every_field_it_names() -> None:
    parsed = rules.parse_rules(
        [{"name": "r", "action": "ignore", "label": ["inbox"], "from": ["nobody@*"]}]
    )
    assert rules.decide(parsed, message()).action == "interpret"


def test_a_pattern_cannot_be_a_regular_expression() -> None:
    parsed = rules.parse_rules([{"name": "r", "action": "ignore", "subject": ["(a+)+$"]}])
    assert rules.decide(parsed, message(subject="a" * 5000)).action == "interpret"


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "r", "action": "ignore", "from": []},
        {"name": "r", "action": "ignore", "from": ["a"] * 21},
        {"name": "r", "action": "ignore", "from": "a"},
        {"name": "r", "action": "ignore", "from": ["x" * 101]},
        {"name": "r!", "action": "ignore", "from": ["a"]},
        {"name": "r", "action": "ignore", "from": ["a"], "colour": "red"},
        {"name": "r", "action": "ignore", "from": ["a"], "task_prefix": "x"},
        {"name": "r", "action": "task", "from": ["a"], "due_in_days": -1},
        {"name": "r", "action": "task", "from": ["a"], "due_in_days": True},
        {"name": "r", "action": "task", "from": ["a"], "task_prefix": "x" * 41},
        "not a mapping",
    ],
)
def test_bad_rules_are_refused_with_a_fixed_error(bad: object) -> None:
    with pytest.raises(PluginError, match="config_invalid"):
        rules.parse_rules([bad])


def test_names_are_unique_and_the_list_is_bounded() -> None:
    one = {"name": "r", "action": "ignore", "from": ["a"]}
    with pytest.raises(PluginError, match="used twice"):
        rules.parse_rules([one, one])
    with pytest.raises(PluginError, match="at most"):
        rules.parse_rules([{**one, "name": f"r{i}"} for i in range(rules.MAX_RULES + 1)])
    assert rules.parse_rules(None) == ()


def test_state_round_trips_and_refuses_corruption(tmp_path: Path) -> None:
    path = str(tmp_path / "s.json")
    assert state.load(path) == {}
    state.save(path, {"b": "2", "a": "1"})
    assert state.load(path) == {"a": "1", "b": "2"}
    Path(path).write_text('{"version": 2, "accounts": {}}', "utf-8")
    with pytest.raises(PluginError, match="state_invalid"):
        state.load(path)
    Path(path).write_bytes(b"x" * (state.MAX_BYTES + 1))
    with pytest.raises(PluginError, match="state_invalid"):
        state.load(path)


def test_state_errors_are_fixed_codes(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="state_unreadable"):
        state.load(str(tmp_path))  # a directory
    with pytest.raises(PluginError, match="state_unwritable"):
        state.save(str(tmp_path / "missing" / "s.json"), {"a": "1"})
