"""The advisory pull-request checks can be switched off; the required check cannot."""

from __future__ import annotations

import re
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


# The duplicate run the workflows' comments describe (a draft marked ready again
# while its run is in progress) comes from these blocks, so they are pinned too.
CONCURRENCY = {
    "benchmarks.yml": {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    },
    "typecheck.yml": {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    },
    "issue-links.yml": {
        "group": "issue-links-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    },
}


@pytest.mark.parametrize("name", sorted(ADVISORY_JOBS))
def test_an_advisory_workflow_keeps_its_concurrency(name: str) -> None:
    workflow = _load(name)
    assert workflow["concurrency"] == CONCURRENCY[name]
    # A job-level block would form a second group with its own cancellation,
    # outside the one pinned above.
    assert all("concurrency" not in job for job in workflow["jobs"].values())


REPO = WORKFLOWS.parents[1]

# A read of a repository variable in an expression. `vars` is matched as a whole
# word, so `vars.NAME`, `vars[...]` (which also takes a name built at run time,
# such as `vars[format('ADVISORY_PR_{0}', 'CHECKS')]`) and `toJSON(vars)` are all
# found.
VARIABLE_READ = re.compile(r"\bvars\b", re.IGNORECASE)

# A read through the REST API, the GitHub CLI or Octokit, from a step or a script.
API_VARIABLE_READ = re.compile(
    r"actions/variables|environments/\S*/variables|(Repo|Org|Environment)Variable"
    r"|\bvariable\s+(get|list|set)\b",
    re.IGNORECASE,
)

# Inputs other than the switch that can skip a required step or turn its failure
# into a pass for some pull requests: secrets are empty on a pull request from a
# fork, and a label is set by whoever triages the pull request.
OTHER_SWITCH = re.compile(r"\bsecrets\b|pull_request\.labels", re.IGNORECASE)


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for key, value in node.items() for s in (*_strings(key), *_strings(value))]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


def _values(node: object, key: str) -> list[object]:
    if isinstance(node, dict):
        return [
            found
            for name, value in node.items()
            for found in ([value] if name == key else []) + _values(value, key)
        ]
    if isinstance(node, list):
        return [found for item in node for found in _values(item, key)]
    return []


def _yaml_files() -> list[Path]:
    # Workflows with either suffix, and any composite action kept under .github.
    root = WORKFLOWS.parent
    return sorted([*root.rglob("*.yml"), *root.rglob("*.yaml")])


def _uncommented(text: str) -> str:
    # A whole-line comment reads nothing. A comment after a value is kept, which
    # can only make the search stricter.
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _reads_a_variable(path: Path) -> bool:
    # The raw text and the parsed strings are both searched: a double-quoted YAML
    # scalar can spell a character as an escape, which only the parse decodes.
    text = path.read_text()
    strings = (_uncommented(text), *_strings(yaml.safe_load(text)))
    return any(VARIABLE_READ.search(s) for s in strings)


def test_only_the_advisory_workflows_read_a_repository_variable() -> None:
    # Any repository variable counts, not only the switch's own name, since a name
    # can be assembled at run time and a required workflow should hold no switch
    # at all. A future need for one in a required workflow is a change to review
    # here.
    files = _yaml_files()
    assert WORKFLOWS / "ci.yml" in files
    readers = sorted(path.name for path in files if _reads_a_variable(path))
    assert readers == sorted(ADVISORY_JOBS)


@pytest.mark.parametrize(
    "line",
    [
        "if: ${{ vars.ADVISORY_PR_CHECKS != 'off' }}",
        "if: ${{ VARS.advisory_pr_checks != 'off' }}",
        "if: ${{ vars[format('ADVISORY_PR_{0}', 'CHECKS')] != 'off' }}",
        "continue-on-error: ${{ fromJSON(toJSON(vars)).ADVISORY_PR_CHECKS == 'off' }}",
        "if: \"${{ \\x76ars.ADVISORY_PR_CHECKS != 'off' }}\"",
    ],
)
def test_each_spelling_of_a_variable_read_is_found(tmp_path: Path, line: str) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(f"jobs:\n  lint:\n    {line}\n")
    assert _reads_a_variable(path)


def test_a_whole_line_comment_is_not_a_variable_read(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text("# Set from the repository vars.\njobs:\n  lint:\n    if: always()\n")
    assert not _reads_a_variable(path)


def test_a_local_action_lives_where_the_variable_search_reads() -> None:
    # A composite action used by path runs inside the job that uses it, so it is
    # searched like a workflow, and the search covers .github only.
    uses = [
        value
        for path in _yaml_files()
        for value in _values(yaml.safe_load(path.read_text()), "uses")
    ]
    assert uses
    assert [
        u
        for u in uses
        if isinstance(u, str) and u.startswith("./") and not u.startswith("./.github/")
    ] == []


def test_no_step_or_script_reads_a_variable_through_the_api() -> None:
    # The REST endpoint, `gh variable get` and the Octokit helpers return the same
    # value as `vars`, to a step's shell or a script it runs.
    files = [
        path
        for root in (WORKFLOWS.parent, REPO / "scripts")
        for path in root.rglob("*")
        if path.is_file()
    ]
    assert WORKFLOWS / "ci.yml" in files
    assert REPO / "scripts" / "check_issue_links.py" in files
    hits = [path for path in files if API_VARIABLE_READ.search(path.read_text(errors="replace"))]
    assert hits == []


@pytest.mark.parametrize(
    "text",
    [
        "gh api repos/${{ github.repository }}/actions/variables/ADVISORY_PR_CHECKS",
        "gh api repos/o/r/environments/ci/variables/ADVISORY_PR_CHECKS",
        "gh variable get ADVISORY_PR_CHECKS",
        "gh variable list --json name,value",
        "await github.rest.actions.getRepoVariable({ owner, repo, name })",
        "await github.rest.actions.listOrgVariables({ org })",
        "await github.rest.actions.getEnvironmentVariable({ owner, repo, environment_name, name })",
    ],
)
def test_each_api_read_of_a_variable_is_found(text: str) -> None:
    assert API_VARIABLE_READ.search(text)


def _conditions(text: str) -> list[str]:
    # Every `if` and `continue-on-error` value, parsed and as written, so a value
    # a duplicate key hides from the parse is read too.
    parsed = yaml.safe_load(text)
    written = [
        line
        for line in text.splitlines()
        if re.match(r"\s*(-\s*)?(if|continue-on-error)\s*:", line)
    ]
    return [
        *(str(v) for key in ("if", "continue-on-error") for v in _values(parsed, key)),
        *written,
    ]


def test_the_required_workflow_has_no_other_switch() -> None:
    conditions = _conditions((WORKFLOWS / "ci.yml").read_text())
    assert any("always()" in c for c in conditions)
    assert [c for c in conditions if OTHER_SWITCH.search(c)] == []


@pytest.mark.parametrize(
    "line",
    [
        "if: ${{ secrets.CODECOV_TOKEN != '' }}",
        "if: ${{ !contains(github.event.pull_request.labels.*.name, 'skip-lint') }}",
        "continue-on-error: ${{ secrets.TOKEN == '' }}",
    ],
)
def test_each_other_switch_is_found(line: str) -> None:
    text = f"jobs:\n  lint:\n    {line}\n"
    assert [c for c in _conditions(text) if OTHER_SWITCH.search(c)]
