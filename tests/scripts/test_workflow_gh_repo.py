"""A checkout-free job that shells out to ``gh`` has to name its repository.

``gh`` resolves the repository from the git remote of its working directory. A
job with no ``actions/checkout`` step has no remote, so a repo-scoped
subcommand exits with ``failed to run git: fatal: not a git repository``
before doing any work.

This is worth a static guard specifically because it survives the obvious
verification. Running a step's script with a stubbed ``gh`` proves the script's
own branching and proves nothing about the CLI, because the stub answers
without ever performing the repository resolution the real CLI performs first.
The precondition is invisible to every test that replaces the dependency.

Lives in its own module rather than joining ``test_ci_hygiene.py``: that module
skips entirely when ripgrep is absent, and a guard that quietly disappears on
some runners is not a guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# Subcommands that resolve a repository from the working directory when no
# --repo flag and no GH_REPO are supplied. `api` is left out on purpose: its
# paths are usually written out in full, so including it would flag calls that
# resolve nothing.
REPO_SCOPED = ("issue", "pr", "release", "repo", "workflow", "run", "label")

_GH_CALL = re.compile(r"\bgh\s+(" + "|".join(REPO_SCOPED) + r")\b")


def _logical_lines(script: str) -> list[str]:
    """Join backslash continuations so a flag on the next line still counts."""
    return script.replace("\\\n", " ").splitlines()


def _job_has_checkout(job: dict) -> bool:
    for step in job.get("steps") or []:
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout"):
            return True
    return False


def gh_repo_violations(text: str) -> list[str]:
    """Return one description per checkout-free ``gh`` call with no repository."""
    document = yaml.safe_load(text) or {}
    workflow_env = document.get("env") or {}
    violations: list[str] = []

    for job_name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict) or _job_has_checkout(job):
            continue
        job_env = job.get("env") or {}
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or not step.get("run"):
                continue
            step_env = step.get("env") or {}
            if any("GH_REPO" in env for env in (step_env, job_env, workflow_env)):
                continue
            for line in _logical_lines(str(step["run"])):
                if _GH_CALL.search(line) and "--repo" not in line:
                    name = step.get("name") or "<unnamed step>"
                    violations.append(f"{job_name} / {name}: {line.strip()}")
    return violations


def test_no_checkout_free_gh_call_is_missing_a_repository() -> None:
    found: list[str] = []
    workflows = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))

    assert workflows, f"no workflow files under {WORKFLOW_DIR}; the check read nothing"

    for path in workflows:
        found.extend(f"{path.name}: {item}" for item in gh_repo_violations(path.read_text()))

    assert not found, "gh calls that cannot resolve a repository:\n" + "\n".join(found)


BROKEN = """
jobs:
  alarm:
    runs-on: ubuntu-latest
    steps:
      - name: File the issue
        env:
          GH_TOKEN: token
        run: |
          gh issue create --title "t" --body "b"
"""


def test_the_check_flags_a_checkout_free_call_with_no_repository() -> None:
    """The broken control. Without this the check could pass by never matching."""
    violations = gh_repo_violations(BROKEN)

    assert len(violations) == 1
    assert "gh issue create" in violations[0]


@pytest.mark.parametrize(
    "cure",
    [
        pytest.param("        env:\n          GH_REPO: owner/name\n", id="gh-repo-env"),
        pytest.param("        env:\n          GH_TOKEN: t\n", id="explicit-repo-flag"),
    ],
)
def test_the_check_accepts_a_call_that_names_its_repository(cure: str) -> None:
    body = BROKEN.replace("        env:\n          GH_TOKEN: token\n", cure)
    if "GH_REPO" not in cure:
        body = body.replace("gh issue create --title", "gh issue create --repo o/n --title")

    assert gh_repo_violations(body) == []


def test_a_job_that_checks_out_is_not_flagged() -> None:
    body = BROKEN.replace(
        "    steps:\n",
        "    steps:\n      - uses: actions/checkout@v5\n",
    )

    assert gh_repo_violations(body) == []
