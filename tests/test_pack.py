# SPDX-License-Identifier: Apache-2.0
"""The proposed `pack-mail-triage` (`docs/pack-mail-triage/`), through the core's own loaders.

The pack is the content of a repository the maintainer has yet to create (#6, item 3): these
checks move with it. The pack must also stay the same pipeline and skill as the ones this plugin
ships (the example pipeline, `gmail/triage-mail`), so a drift between the two fails here."""

import json
from pathlib import Path
from typing import Any

import yaml
from horae.cli import app
from horae.config.cron import cron_problem
from horae.contracts import ActStep, CollectStep, LlmStep, Pipeline
from horae.contracts.plugin import PluginManifest
from horae.skills import load_skill
from typer.testing import CliRunner

from skill import PIPELINE, ROOT, SKILL, read_yaml

PACK = ROOT / "docs" / "pack-mail-triage"
PACK_SKILL = "mail-triage/triage-mail"
PACK_PIPELINE = "mail-triage"
runner = CliRunner()


def _manifest() -> PluginManifest:
    return PluginManifest.model_validate(read_yaml(PACK / "plugin.yaml"))


def _pipeline() -> Pipeline:
    return Pipeline.model_validate(read_yaml(PACK / "pipelines" / f"{PACK_PIPELINE}.yaml"))


def test_the_manifest_is_a_pack_without_runtime_or_access() -> None:
    plugin = _manifest()
    assert plugin.name == "pack-mail-triage"
    assert plugin.runtime is None
    assert plugin.provides.skills == [PACK_SKILL]
    assert plugin.provides.pipelines == [PACK_PIPELINE]
    assert not plugin.provides.sources
    assert not plugin.provides.sinks
    permissions = plugin.permissions
    assert permissions.network == []
    assert permissions.secrets == []
    assert permissions.paths.read == []
    assert permissions.paths.write == []
    assert permissions.intents == ["task.create"]


def test_every_provided_pipeline_has_its_file_and_nothing_else_is_shipped() -> None:
    files = sorted(path.stem for path in (PACK / "pipelines").glob("*.yaml"))
    assert files == sorted(_manifest().provides.pipelines)
    assert _pipeline().name == PACK_PIPELINE


def test_the_pipeline_is_collect_llm_act_and_runs_the_packs_skill() -> None:
    mail = _pipeline()
    assert mail.trigger is not None
    assert cron_problem(mail.trigger.cron) is None
    assert [type(step) for step in mail.steps] == [CollectStep, LlmStep, ActStep]
    llm = mail.steps[1]
    assert isinstance(llm, LlmStep)
    assert llm.llm.skill == PACK_SKILL


def test_the_pack_pipeline_is_the_example_pipeline_under_the_packs_names() -> None:
    """Same steps, parameters and budgets: only the pipeline's and the skill's names differ."""
    example = read_yaml(PIPELINE)
    packed = read_yaml(PACK / "pipelines" / f"{PACK_PIPELINE}.yaml")
    example["name"] = PACK_PIPELINE
    example["steps"][1]["llm"]["skill"] = PACK_SKILL
    assert packed == example


def test_the_pack_skill_is_the_plugins_skill_under_the_packs_name() -> None:
    ours = ROOT / "skills" / SKILL
    theirs = PACK / "skills" / PACK_SKILL
    for name in ("prompt.md", "output.schema.json"):
        assert (theirs / name).read_bytes() == (ours / name).read_bytes(), name
    ours_yaml: dict[str, Any] = read_yaml(ours / "skill.yaml")
    ours_yaml["name"] = PACK_SKILL
    assert read_yaml(theirs / "skill.yaml") == ours_yaml
    cases = sorted(p.name for p in (ours / "evals").glob("*.yaml"))
    assert cases == sorted(p.name for p in (theirs / "evals").glob("*.yaml"))
    for name in cases:
        assert (theirs / "evals" / name).read_bytes() == (ours / "evals" / name).read_bytes()


def test_the_pack_skill_loads_and_every_golden_case_passes_offline_none_stale() -> None:
    plugin = _manifest()
    loaded = load_skill(PACK, PACK_SKILL, allowed_intents=plugin.permissions.intents)
    assert loaded.name == PACK_SKILL
    result = runner.invoke(app, ["eval", PACK_SKILL, "--plugin-dir", str(PACK), "--json"])
    report = json.loads(result.stdout)
    failed = [(c["case"], c["status"], c["error"]) for c in report["cases"]]
    assert result.exit_code == 0, failed
    summary = report["summary"]
    assert summary["cases"] == summary["passed"] == 13
    assert summary["stale"] == 0, [(c["case"], c["stale"]) for c in report["cases"]]


def test_the_pack_has_no_symlink_and_only_regular_yaml_pipelines() -> None:
    for path in PACK.rglob("*"):
        assert not path.is_symlink(), path
    assert all(p.is_file() for p in Path(PACK / "pipelines").iterdir())
    assert yaml.safe_load((PACK / "plugin.yaml").read_text("utf-8"))["name"] == "pack-mail-triage"
