# SPDX-License-Identifier: Apache-2.0
"""The workflows' safety rules: what runs on a pull request and what can spend money.

Plain YAML checks, no GitHub: the live eval spends money and holds a model key, so it must run
only on a release tag, only when the key exists, behind the `evals` environment, with a spend
cap, the one write permission on a job without the key, and every action pinned to a commit."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
CORE = re.compile(r"^horae @ git\+https://github\.com/horae-dev/horae@[0-9a-f]{40}$")


def _workflow(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))
    assert isinstance(workflow, dict)
    workflow["on"] = workflow.pop(True, workflow.get("on"))  # YAML reads a bare `on` as True
    return workflow


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_the_live_eval_runs_only_on_release_tags_behind_the_evals_environment() -> None:
    release = _workflow("release-evals.yml")
    assert release["on"] == {"push": {"tags": ["v*"]}}
    assert release["permissions"] == {}
    evaluation = release["jobs"]["eval"]
    assert evaluation["environment"] == "evals"
    assert evaluation["permissions"] == {"contents": "read"}
    assert "ANTHROPIC_API_KEY" not in str(release["jobs"]["attach"])


def test_the_spend_is_capped_and_the_cap_is_passed_to_every_live_case() -> None:
    release = _workflow("release-evals.yml")
    assert re.fullmatch(r"\d+(\.\d+)?", release["env"]["EVAL_MAX_USD"])
    assert float(release["env"]["EVAL_MAX_USD"]) > 0
    script = (ROOT / "scripts" / "live-eval.sh").read_text(encoding="utf-8")
    assert "--live --yes --max-usd" in script
    assert "--record" not in script.replace("`--record` is never passed", "")


def test_without_the_key_every_step_but_the_check_is_skipped() -> None:
    evaluation = _workflow("release-evals.yml")["jobs"]["eval"]
    check, *rest = evaluation["steps"]
    assert check["id"] == "key"
    assert "-n" in check["run"]
    assert rest
    for step in rest:
        assert "steps.key.outputs.present == 'true'" in step["if"], step
    attach = _workflow("release-evals.yml")["jobs"]["attach"]
    assert "needs.eval.outputs.ran == 'true'" in attach["if"]


def test_only_the_attach_job_can_write_and_the_key_is_two_steps_only() -> None:
    release = _workflow("release-evals.yml")
    writers = [
        n for n, j in release["jobs"].items() if "write" in j.get("permissions", {}).values()
    ]
    assert writers == ["attach"]
    with_key = [
        s for s in release["jobs"]["eval"]["steps"] if "ANTHROPIC_API_KEY" in str(s.get("env", ""))
    ]
    assert [s.get("id", s.get("name")) for s in with_key] == [
        "key",
        "Send the golden cases to the model",
    ]
    assert "env" not in release["jobs"]["eval"]


def test_the_live_eval_never_runs_on_a_pull_request() -> None:
    ci = _workflow("ci.yml")
    assert "pull_request" in ci["on"]
    assert "--live" not in yaml.safe_dump(ci)
    assert "pull_request" not in _workflow("release-evals.yml")["on"]


def test_the_core_is_pinned_to_a_commit_the_same_in_both_workflows() -> None:
    ci, release = _workflow("ci.yml"), _workflow("release-evals.yml")
    assert CORE.match(ci["env"]["HORAE_CORE"])
    assert ci["env"]["HORAE_CORE"] == release["env"]["HORAE_CORE"]
    assert ci["env"]["HORAE_REQUIRED"] == "1"


def test_every_action_is_pinned_and_no_run_step_interpolates_an_expression() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for step in _steps(_workflow(path.name)):
            if "uses" in step:
                assert SHA.match(step["uses"]), (path.name, step["uses"])
            assert "${{" not in step.get("run", ""), (path.name, step.get("name"))


def test_the_pyproject_has_no_git_source_and_no_exclude_newer() -> None:
    """The core's installer refuses a git source in the lock and exclude-newer breaks installs."""
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    assert "horae-sdk>=0.9,<0.10" in text
    assert "exclude-newer" not in text
    assert "[tool.uv.sources]" not in text
    lock = (ROOT / "uv.lock").read_text("utf-8")
    assert "git = " not in lock
    assert "exclude-newer" not in lock
