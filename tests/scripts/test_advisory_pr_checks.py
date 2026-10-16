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


@pytest.mark.parametrize("name", sorted(ADVISORY_JOBS))
def test_an_advisory_job_runs_when_a_draft_is_marked_ready(name: str) -> None:
    types = _triggers(_load(name))["pull_request"]["types"]
    assert {"opened", "synchronize", "reopened", "ready_for_review"} <= set(types)


def test_only_the_advisory_workflows_read_the_switch() -> None:
    readers = sorted(
        path.name for path in WORKFLOWS.glob("*.yml") if "ADVISORY_PR_CHECKS" in path.read_text()
    )
    assert readers == sorted(ADVISORY_JOBS)
    assert "ADVISORY_PR_CHECKS" not in (WORKFLOWS / "ci.yml").read_text()
