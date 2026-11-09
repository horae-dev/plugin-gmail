# SPDX-License-Identifier: Apache-2.0
"""The user's rules: configuration (trusted) matched against a message (untrusted), no LLM.

A rule has a `name`, an `action` and matchers on the sender's address (`from`), the subject
(`subject`), the labels (`label`, Gmail label ids such as `CATEGORY_PROMOTIONS`) and the
`List-Id` (`list_id`). Each matcher is a list of case-insensitive `fnmatch` patterns (`*`, `?`,
`[...]`); a field matches when any of its patterns does, and the rule matches when every field
it names does. At least one matcher is required, so no rule matches everything by accident.
Rules are tried in order and **the first match wins: every message gets exactly one decision**.

- `ignore`: nothing happens. Decided by the rules.
- `task`: a `task.create` is proposed (fingerprint `gmail:<message id>`, the Gmail link, a title
  of `task_prefix` and the subject, `due_in_days` after the message's day). Decided by the rules.
- `interpret`: the rules route the message to the LLM step on purpose (for example school mail),
  before a later `ignore` rule could take it. Not decided by the rules.

A message no rule matches **needs interpretation** too (`action: interpret`, `rule: null`).

Patterns are globs, not regular expressions, and run on fields cut to `MATCH_CHARS`, so a
hostile message cannot make a rule slow.
"""

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from horae_sdk import PluginError

from horae_gmail.mail import MATCH_CHARS, Message

MAX_RULES = 64
MAX_PATTERNS = 20
MAX_PATTERN = 100
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
FIELDS = ("from", "subject", "label", "list_id")
KEYS = frozenset({"name", "action", "task_prefix", "due_in_days", *FIELDS})
ACTIONS = ("ignore", "task", "interpret")
Action = Literal["ignore", "task", "interpret"]
MAX_DUE_DAYS = 365


@dataclass(frozen=True)
class Rule:
    name: str
    action: Action
    patterns: tuple[tuple[str, tuple[str, ...]], ...]
    """`(field, lower-case patterns)` for each matcher the rule names."""
    task_prefix: str
    due_in_days: int | None


@dataclass(frozen=True)
class Decision:
    """The one decision of a message."""

    action: Action
    rule: Rule | None

    @property
    def decided(self) -> bool:
        """Whether the rules settled the message (no LLM step needed for it)."""
        return self.action != "interpret"


def parse_rules(value: object) -> tuple[Rule, ...]:
    """The `rules` parameter, checked. Errors name the field, never a message."""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) > MAX_RULES:
        raise PluginError(f"config_invalid: rules must be a list of at most {MAX_RULES}")
    rules: list[Rule] = []
    seen: set[str] = set()
    for position, spec in enumerate(value):
        where = f"config_invalid: rules[{position}]"
        if not isinstance(spec, Mapping):
            raise PluginError(f"{where} must be a mapping")
        unknown = set(spec) - KEYS
        if unknown:
            raise PluginError(f"{where}: unknown key {sorted(map(str, unknown))[0]!r}")
        name, action = spec.get("name"), spec.get("action")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise PluginError(f"{where}.name must be 1 to 40 of A-Z a-z 0-9 . _ -")
        if name in seen:
            raise PluginError(f"{where}.name is used twice")
        if action not in ACTIONS:
            raise PluginError(f"{where}.action must be ignore, task or interpret")
        prefix = spec.get("task_prefix", "")
        if not isinstance(prefix, str) or len(prefix) > 40:
            raise PluginError(f"{where}.task_prefix must be a string of at most 40 characters")
        due = spec.get("due_in_days")
        if due is not None and (type(due) is not int or not 0 <= due <= MAX_DUE_DAYS):
            raise PluginError(
                f"{where}.due_in_days must be a whole number from 0 to {MAX_DUE_DAYS}"
            )
        if action != "task" and (prefix or due is not None):
            raise PluginError(f"{where}: task_prefix and due_in_days belong to action task")
        patterns: list[tuple[str, tuple[str, ...]]] = []
        for field in FIELDS:
            if field not in spec:
                continue
            given = spec[field]
            if (
                not isinstance(given, Sequence)
                or isinstance(given, str)
                or not 1 <= len(given) <= MAX_PATTERNS
                or not all(isinstance(p, str) and 1 <= len(p) <= MAX_PATTERN for p in given)
            ):
                raise PluginError(
                    f"{where}.{field} must list 1 to {MAX_PATTERNS} patterns "
                    f"of 1 to {MAX_PATTERN} characters"
                )
            patterns.append((field, tuple(p.lower() for p in given)))
        if not patterns:
            raise PluginError(f"{where} needs at least one of from, subject, label, list_id")
        seen.add(name)
        rules.append(Rule(name, action, tuple(patterns), prefix, due))
    return tuple(rules)


def _values(message: Message, field: str) -> tuple[str, ...]:
    """What a matcher is tried against; a field the message does not have gives nothing, so
    `*` means "has one" (`list_id: ["*"]` is any mailing list), never "anything"."""
    if field == "label":
        return tuple(label.lower() for label in message.labels)
    one = {"from": message.sender, "subject": message.subject, "list_id": message.list_id}
    value = one[field][:MATCH_CHARS].lower()
    return (value,) if value.strip() else ()


def matches(rule: Rule, message: Message) -> bool:
    return all(
        any(fnmatch.fnmatchcase(value, p) for value in _values(message, field) for p in patterns)
        for field, patterns in rule.patterns
    )


def decide(rules: Sequence[Rule], message: Message) -> Decision:
    """The decision of `message`: the first matching rule's, else `interpret` without a rule."""
    for rule in rules:
        if matches(rule, message):
            return Decision(rule.action, rule)
    return Decision("interpret", None)
