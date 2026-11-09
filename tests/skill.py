# SPDX-License-Identifier: Apache-2.0
"""Paths and loaders shared by the skill tests: everything is read with the core's own code."""

from pathlib import Path
from typing import Any

import yaml
from horae.contracts import Pipeline
from horae.contracts.plugin import PluginManifest
from horae.evals import LoadedCase, evals_dir, load_cases
from horae.skills import Skill, load_skill

ROOT = Path(__file__).resolve().parent.parent
SKILL = "gmail/triage-mail"
PIPELINE = ROOT / "examples" / "triage-mail.pipeline.yaml"


def read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def manifest() -> PluginManifest:
    return PluginManifest.model_validate(read_yaml(ROOT / "plugin.yaml"))


def skill() -> Skill:
    return load_skill(ROOT, SKILL, allowed_intents=manifest().permissions.intents)


def pipeline() -> Pipeline:
    return Pipeline.model_validate(read_yaml(PIPELINE))


def cases() -> dict[str, LoadedCase]:
    return {loaded.name: loaded for loaded in load_cases(evals_dir(ROOT, SKILL))}


def message_ids(case_input: Any) -> set[str]:
    """The ids of the messages a case shows the model."""
    found: set[str] = set()
    for entry in case_input:
        for message in entry["data"]["needs_interpretation"]:
            found.add(message["id"])
    return found


def referenced_ids(output: dict[str, Any]) -> set[str]:
    """Every message id an answer cites: in `triaged` and in the fingerprints of its intents."""
    cited = {item["message_id"] for item in output["triaged"]}
    cited |= {
        intent["fingerprint"].removeprefix("gmail:")
        for intent in output.get("intents", [])
        if intent["type"] == "task.create"
    }
    return cited
