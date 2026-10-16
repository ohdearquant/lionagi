"""The advisory pull-request checks can be switched off; the required check cannot."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

SWITCH = "vars.ADVISORY_PR_CHECKS != 'off' && !github.event.pull_request.draft"

# Workflow file -> (job, its condition). Issue links runs only on pull requests,
# so its condition has no event clause.
ADVISORY_JOBS = {
    "benchmarks.yml": (
        "run-benchmarks",
        f"github.event_name != 'pull_request' || ({SWITCH})",
    ),
    "typecheck.yml": (
        "pyright",
        f"github.event_name != 'pull_request' || ({SWITCH})",
    ),
    "issue-links.yml": ("closing-keywords", SWITCH),
}


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


@pytest.mark.parametrize("name", sorted(ADVISORY_JOBS))
def test_an_advisory_job_skips_drafts_and_obeys_the_switch(name: str) -> None:
    job, condition = ADVISORY_JOBS[name]
    workflow = _load(name)
    assert workflow["jobs"][job]["if"] == condition
    assert set(workflow["jobs"]) == {job}


# Each workflow's whole trigger block. Pinned exactly rather than by the types it
# must include: dropping `edited` from Issue links would stop the re-run that
# fixing a PR body depends on, and dropping `push` from the other two would stop
# their record on the protected branches, and a subset check passes both.
PULL_REQUEST_TYPES = ["opened", "synchronize", "reopened", "ready_for_review"]
TRIGGERS = {
    "benchmarks.yml": {
        "push": {"branches": ["main", "master"]},
        "pull_request": {"types": PULL_REQUEST_TYPES, "branches": ["main", "master"]},
    },
    "typecheck.yml": {
        "push": {"branches": ["main", "develop"], "paths-ignore": ["**/*.ipynb"]},
        "pull_request": {
            "types": PULL_REQUEST_TYPES,
            "branches": ["main", "develop"],
            "paths-ignore": ["**/*.ipynb"],
        },
    },
    "issue-links.yml": {
        "pull_request": {
            "types": ["opened", "edited", "reopened", "synchronize", "ready_for_review"],
            "branches": ["main", "develop"],
        },
    },
}


@pytest.mark.parametrize("name", sorted(ADVISORY_JOBS))
def test_an_advisory_workflow_keeps_its_triggers(name: str) -> None:
    # ready_for_review is among them: the job skips drafts, so a draft marked
    # ready would otherwise get no run until its next push.
    assert _triggers(_load(name)) == TRIGGERS[name]


def test_only_the_advisory_workflows_read_the_switch() -> None:
    # GitHub loads workflows with either suffix.
    workflows = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    readers = sorted(path.name for path in workflows if "ADVISORY_PR_CHECKS" in path.read_text())
    assert readers == sorted(ADVISORY_JOBS)
    assert "ADVISORY_PR_CHECKS" not in (WORKFLOWS / "ci.yml").read_text()
