"""The advisory pull-request checks can be switched off; the required check cannot.

These tests read the repository's own workflow files, so they catch a switch
added to a required workflow by accident. A pull request that edits those files
can also edit this test, so they do not stop a deliberate one.
"""

from __future__ import annotations

import posixpath
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
# The REST forms need the `repos/` or `orgs/` path, so a link to GitHub's
# documentation of the endpoint is not one. A path assembled at run time from
# pieces is not found.
API_VARIABLE_READ = re.compile(
    r"\b(repos|orgs)/[^\n]*/actions/variables|\benvironments/[^\n]*/variables\b"
    r"|\b(get|list)(Repo|Org|Environment)Variables?\b|\bgh\s+variable\s+(get|list|set)\b",
    re.IGNORECASE,
)

# Inputs other than the switch that can skip a required step, pass its failure or
# change what it runs for some pull requests: a label is set by whoever triages a
# pull request, a secret is empty on a pull request from a fork, and the event's
# details (draft, fork, author) differ from one pull request to the next.
# `github.event_name` is the same for every pull request and stays allowed.
LABEL = re.compile(r"\blabels?\b", re.IGNORECASE)
SECRET = re.compile(r"\bsecrets\b", re.IGNORECASE)
EVENT_DETAIL = re.compile(
    r"\bgithub\s*(\.\s*(event(?!_name)|actor|triggering_actor|head_ref)\b|\[)", re.IGNORECASE
)


def _strings(node: object, skip: str | None = None) -> list[str]:
    # Every string in a parsed document, keys included, leaving out what sits
    # under a key named *skip*.
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [
            s
            for key, value in node.items()
            if key != skip
            for s in (*_strings(key, skip), *_strings(value, skip))
        ]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item, skip)]
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
    # A whole-line YAML comment reads nothing. A comment after a value is kept,
    # and so is a `#` line inside a block scalar, which is part of its string;
    # both can only make the search stricter.
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


def _local_target(uses: str) -> str | None:
    """The repository path a local ``uses:`` names, with ``.`` and ``..`` resolved."""
    return posixpath.normpath(uses[2:]) if uses.startswith("./") else None


def test_a_local_action_lives_where_the_variable_search_reads() -> None:
    # A composite action used by path runs inside the job that uses it, so it is
    # searched like a workflow, and the search covers .github only. The path is
    # read after resolving `..`, so `./.github/../actions/x` counts as outside.
    uses = [
        value
        for path in _yaml_files()
        for value in _values(yaml.safe_load(path.read_text()), "uses")
    ]
    assert uses
    outside = [
        u
        for u in uses
        if isinstance(u, str)
        and (target := _local_target(u)) is not None
        and not target.startswith(".github/")
    ]
    assert outside == []


@pytest.mark.parametrize(
    ("uses", "target"),
    [
        ("./.github/actions/setup", ".github/actions/setup"),
        ("./.github/./actions//setup", ".github/actions/setup"),
        ("./.github/../actions/switch", "actions/switch"),
        ("./../elsewhere", "../elsewhere"),
        ("actions/checkout@v4", None),
    ],
)
def test_a_local_path_is_read_with_dot_segments_resolved(uses: str, target: str | None) -> None:
    assert _local_target(uses) == target


def test_no_step_or_script_reads_a_variable_through_the_api() -> None:
    # The REST endpoint, `gh variable get` and the Octokit helpers return the same
    # value as `vars`, to a step's shell, a script it runs or a test the required
    # `test` job runs. This file names those forms and is left out.
    files = [
        path
        for root in (WORKFLOWS.parent, REPO / "scripts", REPO / "tests")
        for path in root.rglob("*")
        if path.is_file() and path != Path(__file__).resolve()
    ]
    assert WORKFLOWS / "ci.yml" in files
    assert REPO / "scripts" / "check_issue_links.py" in files
    assert REPO / "tests" / "conftest.py" in files
    hits = [path for path in files if API_VARIABLE_READ.search(path.read_text(errors="replace"))]
    assert hits == []


@pytest.mark.parametrize(
    "text",
    [
        "gh api repos/${{ github.repository }}/actions/variables/ADVISORY_PR_CHECKS",
        "gh api orgs/o/actions/variables",
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


@pytest.mark.parametrize(
    "text",
    [
        "# The endpoint: https://docs.github.com/en/rest/actions/variables",
        "The variable set to 1 turns it off.",
        "a variable list of names",
        "raise MissingEnvironmentVariable(name)",
    ],
)
def test_prose_about_variables_is_not_an_api_read(text: str) -> None:
    assert not API_VARIABLE_READ.search(text)


def _required_files() -> list[Path]:
    """``ci.yml`` and every local action or reusable workflow it uses, however deep."""
    found: list[Path] = []
    pending = [WORKFLOWS / "ci.yml"]
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.append(path)
        for uses in _values(yaml.safe_load(path.read_text()), "uses"):
            target = _local_target(uses) if isinstance(uses, str) else None
            if target is None:
                continue
            local = REPO / target
            if local.suffix in (".yml", ".yaml"):
                pending.append(local)
                continue
            actions = [local / name for name in ("action.yml", "action.yaml")]
            actions = [action for action in actions if action.is_file()]
            assert actions, f"{uses} names no action.yml"
            pending.extend(actions)
    return found


def _other_switches(text: str) -> list[str]:
    """What in a required workflow or action could act as a switch.

    Labels and event details are refused anywhere in the file, in the raw text
    without whole-line comments and in the parsed strings, so an `env`, `run`,
    `runs-on` or matrix value counts as much as an `if`. A secret is allowed only
    as an action's `with:` input, which is how a step receives one, and the raw
    text may hold no more of them than those inputs do, so a duplicate key that
    hides one from the parse is still found.
    """
    parsed = yaml.safe_load(text)
    raw = _uncommented(text)
    found = [
        match.group(0)
        for pattern in (LABEL, EVENT_DETAIL)
        for string in (raw, *_strings(parsed))
        for match in pattern.finditer(string)
    ]
    inputs = [s for value in _values(parsed, "with") for s in _strings(value)]
    allowed = sum(len(SECRET.findall(s)) for s in inputs)
    outside = [s for s in _strings(parsed, skip="with") if SECRET.search(s)]
    if outside or len(SECRET.findall(raw)) > allowed:
        found.append("secrets outside a with: input")
    return found


def test_the_required_workflow_has_no_other_switch() -> None:
    files = _required_files()
    assert files[0] == WORKFLOWS / "ci.yml"
    # The one secret ci.yml uses reaches an action as an input, so the real file
    # exercises the allowance.
    ci = yaml.safe_load(files[0].read_text())
    assert any(SECRET.search(s) for value in _values(ci, "with") for s in _strings(value))
    switches = {str(path.relative_to(REPO)): _other_switches(path.read_text()) for path in files}
    assert switches == {name: [] for name in switches}


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  lint:\n    if: ${{ secrets.CODECOV_TOKEN != '' }}\n",
        "jobs:\n  lint:\n    continue-on-error: ${{ secrets.TOKEN == '' }}\n",
        "jobs:\n  lint:\n    if: ${{ !contains(github.event.pull_request.labels.*.name, 'x') }}\n",
        "jobs:\n  lint:\n    if: ${{ !contains(github.event.pull_request['labels'].*.name, 'x') }}\n",
        "jobs:\n  lint:\n    steps:\n      - env:\n"
        "          SKIP: ${{ contains(github.event.pull_request.labels.*.name, 'x') }}\n"
        "        if: env.SKIP != 'true'\n        run: make lint\n",
        "jobs:\n  lint:\n    steps:\n      - run: |\n"
        "          gh pr view --json labels | grep -q skip && exit 0\n",
        "jobs:\n  lint:\n    runs-on: ${{ github.event.label.name == 'x' && 'none' || 'ubuntu' }}\n",
        "jobs:\n  lint:\n    if: ${{ !github.event.pull_request.draft }}\n",
        "jobs:\n  lint:\n    if: ${{ github.actor != 'dependabot[bot]' }}\n",
        "jobs:\n  lint:\n    if: ${{ github['event']['pull_request']['head']['repo']['fork'] }}\n",
        "jobs:\n  lint:\n    steps:\n      - env:\n          TOKEN: ${{ secrets.TOKEN }}\n"
        '        run: test -n "$TOKEN" || exit 0\n',
        "jobs:\n  lint:\n    steps:\n"
        "      - {if: \"${{ secrets.X != '' }}\", if: always(), run: make lint}\n",
    ],
    ids=[
        "secret-if",
        "secret-continue-on-error",
        "label-if",
        "label-bracket",
        "label-through-env",
        "label-in-run",
        "label-runs-on",
        "draft",
        "actor",
        "event-bracket",
        "secret-through-env",
        "secret-hidden-by-duplicate-key",
    ],
)
def test_each_other_switch_is_found(text: str) -> None:
    assert _other_switches(text)


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  test:\n    steps:\n      - uses: codecov/codecov-action@v4\n"
        "        with:\n          token: ${{ secrets.CODECOV_TOKEN }}\n",
        "jobs:\n  test:\n    if: github.event_name != 'pull_request'\n",
        "# Labels and secrets are read nowhere here.\njobs:\n  test:\n    if: always()\n",
    ],
    ids=["secret-as-input", "event-name", "comment"],
)
def test_what_a_required_workflow_may_hold(text: str) -> None:
    assert _other_switches(text) == []
