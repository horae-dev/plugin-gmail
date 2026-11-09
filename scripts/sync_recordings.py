# SPDX-License-Identifier: Apache-2.0
"""Refresh the request digests of the hand-written golden answers (the skill `gmail/triage-mail`).

The golden answers under `skills/<skill>/evals/recorded/<backend>/` hold, besides the output,
the digests of what was sent (the prompt, the viewed input, the output schema) and the
skill's digest and version (schemas decision 48 C). A recording whose digests differ from
the current skill is **stale**. A recording made by a model (`horae eval --live --record`)
must be re-recorded when that happens; this script is only for the **synthetic** answers
this repository writes by hand: it recomputes the digests the way `horae eval` renders each
case (the real provider backend's `render`, offline, nothing sent) and rewrites them,
leaving the output, usage, model and stop reason as they are.

    uv run python scripts/sync_recordings.py          # rewrite stale digests
    uv run python scripts/sync_recordings.py --check  # exit 1 if any is stale

A **manual helper**: it uses only the core's public `horae.evals.request_digest`. CI checks
staleness with the `horae eval --json` report (`summary.stale`), not with this script.
"""

import argparse
import sys
from pathlib import Path

import yaml
from horae.contracts.plugin import PluginManifest
from horae.evals import (
    SkillUnderEval,
    evals_dir,
    load_cases,
    request_digest,
    write_recorded,
)
from horae.skills import load_skill

ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC_MODEL = "claude-fixture-1"
"""The `model` of a hand-written answer. Any other model's recording is never rewritten: a
stale one is reported (re-record it with `horae eval --live --record`)."""


def manifest(root: Path = ROOT) -> PluginManifest:
    with (root / "plugin.yaml").open(encoding="utf-8") as handle:
        return PluginManifest.model_validate(yaml.safe_load(handle))


def sync(root: Path = ROOT, *, check: bool = False) -> list[str]:
    """The `<skill>:<case>:<backend>` recordings that were stale (rewritten unless `check`)."""
    plugin = manifest(root)
    stale: list[str] = []
    for name in plugin.provides.skills:
        skill = load_skill(root, name, allowed_intents=plugin.permissions.intents)
        sue = SkillUnderEval(skill, plugin)
        folder = evals_dir(root, name)
        for loaded in load_cases(folder):
            for backend, recorded in sorted(loaded.recorded.items()):
                label = f"{name}:{loaded.name}:{backend}"
                request = request_digest(sue, loaded, backend=backend, model=recorded.model)
                fresh = recorded.model_copy(
                    update={
                        "request": request,
                        "skill_digest": skill.digest,
                        "skill_version": skill.version,
                    }
                )
                if fresh == recorded:
                    continue
                if recorded.model != SYNTHETIC_MODEL:
                    stale.append(f"{label} (recorded from {recorded.model}: re-record it)")
                    continue
                stale.append(label)
                if not check:
                    write_recorded(folder, loaded.name, fresh)
    return stale


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only report stale recordings")
    args = parser.parse_args()
    stale = sync(check=args.check)
    for label in stale:
        print(f"{'stale' if args.check or 're-record' in label else 'refreshed'}: {label}")
    if any(args.check or "re-record" in label for label in stale):
        sys.exit(1)


if __name__ == "__main__":
    main()
