"""The advisory pull-request checks can be switched off; the required check cannot.

These tests read the required workflow, the scripts it runs and the action code
it uses, so they catch a switch added to any of them by accident. A pull request
that edits those files can also edit this test, so they do not stop a deliberate
one.
"""

from __future__ import annotations

import ast
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
    return _parse((WORKFLOWS / name).read_text())


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

# A read of a repository variable in an expression. `vars` is matched as a
# reference (`vars.NAME`, `vars['NAME']`, `vars[...]`, `toJSON(vars)`) and as a
# whole context inside `${{ }}` (`${{ vars }}`, `format('{0}', vars)`), so prose,
# a step name or a trailing comment that merely contains the word is not a read.
VARIABLE_READ = re.compile(
    r"\bvars\s*\.\s*[A-Za-z_$]|\bvars\s*\[|toJSON\s*\(\s*vars\s*\)"
    r"|\$\{\{(?:(?!\}\}).)*\bvars\b",
    re.IGNORECASE | re.DOTALL,
)

# A read through the REST API, the GitHub CLI or Octokit, from a step or a script.
# Only the read forms count: `gh variable get|list` and the Octokit `get`/`list`
# helpers return the value. A write to the same endpoint (`gh variable set`, a
# PATCH) does not, so it is not one.
API_VARIABLE_READ = re.compile(
    r"\b(repos|orgs)/[^\n]*/actions/variables|\benvironments/[^\n]*/variables\b"
    r"|\b(get|list)(Repo|Org|Environment)Variables?\b|\bgh\s+variable\s+(get|list)\b",
    re.IGNORECASE,
)

# A write verb beside the variables endpoint. The REST path alone does not say
# whether a call reads or writes, so a line that names a write verb is left alone.
# `curl` spells the verb `--request`, and either option may carry it as
# `--method=VERB`.
API_VARIABLE_WRITE = re.compile(
    r"(-X|--method|--request)\s*=?\s*['\"]?(PATCH|POST|PUT|DELETE)\b"
    r"|\b(create|update|delete)(Repo|Org|Environment)Variables?\b",
    re.IGNORECASE,
)

# Inputs other than the switch that can skip a required step, pass its failure or
# change what it runs for some pull requests: a label is set by whoever triages a
# pull request, a secret is empty on a pull request from a fork, and the event's
# details (draft, fork, author, base branch) differ from one pull request to the
# next. `github.event_name` is the same for every pull request and stays allowed.
#
# A label and an event detail are matched only as references, so a step name that
# reads "Check release labels" or prose in a comment is not one. A step that runs
# `actions/github-script` reads the same details as `context.actor`,
# `context.payload.pull_request` and `context.payload.label`.
LABEL = re.compile(
    r"\bgithub\s*\.\s*event\s*\.\s*label\b"
    r"|\bpull_request\s*\[\s*['\"]labels['\"]\s*\]"
    r"|\bpull_request\s*\.\s*labels\b"
    r"|\blabels\s*\.\s*\*"
    r"|--json\s+labels\b"
    r"|\bcontext\s*\.\s*payload\s*\.\s*label\b",
    re.IGNORECASE,
)
SECRET = re.compile(r"\bsecrets\s*\.\s*[A-Za-z_$]|\bsecrets\s*\[", re.IGNORECASE)
EVENT_DETAIL = re.compile(
    r"\bgithub\s*(\.\s*(event_path|event(?!_name)|actor_id|actor|triggering_actor"
    r"|head_ref|base_ref)\b|\[)"
    r"|toJSON\s*\(\s*github\s*\)"
    r"|\bcontext\s*\.\s*(actor\b|payload\s*\.\s*pull_request\b)",
    re.IGNORECASE,
)

# The names GitHub sets for the same details, read from a shell or a script.
ENV_DETAIL = re.compile(
    r"\bGITHUB_(EVENT_PATH|ACTOR_ID|ACTOR|TRIGGERING_ACTOR|HEAD_REF|BASE_REF)\b",
    re.IGNORECASE,
)

# An action or reusable workflow reached from the required workflow reads its own
# inputs. A condition on one of them can switch off the step the caller meant to
# run.
INPUT_READ = re.compile(r"\binputs\s*(\.|\[)", re.IGNORECASE)


def _strings(node: object, skip: str | None = None, reusable_secrets: bool = False) -> list[str]:
    # Every string in a parsed document, keys included, leaving out what sits
    # under a key named *skip*. With *reusable_secrets*, the `secrets:` mapping
    # of a job that calls a reusable workflow is left out too: it hands secrets
    # over rather than reading one.
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        handed_over = reusable_secrets and "uses" in node
        return [
            s
            for key, value in node.items()
            if key != skip and not (handed_over and key == "secrets")
            for s in (
                *_strings(key, skip, reusable_secrets),
                *_strings(value, skip, reusable_secrets),
            )
        ]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item, skip, reusable_secrets)]
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


class _DuplicateKeyError(ValueError):
    """A YAML mapping repeats a key, so a parser would keep only one of the two."""


class _StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = []
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise _DuplicateKeyError(str(key))
            keys.append(key)
        return super().construct_mapping(node, deep=deep)


def _parse(text: str):
    # A repeated mapping key is refused rather than resolved: PyYAML keeps the
    # last one, which would hide whatever the first held from every scan here.
    return yaml.load(text, Loader=_StrictLoader)


# Files that never carry a readable instruction: bytecode caches, compiled
# extensions, archives, images and databases. Read as text, such a file can match
# a literal inside the source it was built from, which is how a cached copy of
# this module's strings reads as a live API call.
NON_SOURCE_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dylib",
        ".dll",
        ".o",
        ".a",
        ".zip",
        ".gz",
        ".tar",
        ".whl",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".woff",
        ".woff2",
        ".parquet",
        ".bin",
        ".db",
    }
)


def _is_source_file(path: Path) -> bool:
    # The searches read with `errors="replace"`, so nothing here decodes the
    # file: one byte outside UTF-8 does not make a script unreadable, and a file
    # dropped for it would escape both scans.
    if "__pycache__" in path.parts:
        return False
    return path.suffix.lower() not in NON_SOURCE_SUFFIXES


def _scanned_files(roots: tuple[Path, ...]) -> list[Path]:
    """Every text source file under *roots*, this module's own file left out."""
    own = Path(__file__).resolve()
    return [
        path
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.resolve() != own and _is_source_file(path)
    ]


def _uncommented(text: str) -> str:
    # Drop whole-line comments, including a `#` line inside a block scalar, which
    # is a comment to the shell that runs the block. Text after a value stays, so
    # the raw search is never less strict than the parse except on comment lines.
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _code_only(text: str) -> str:
    """*text* with whole-line comments and a Python docstring taken out.

    Only code can read a label or an event: a `#` line and a string that stands
    alone as a statement (a module, class or function docstring) say what the
    code is for. A Python file is read through `ast`, so such a string is
    dropped; text that does not parse as Python keeps its `#` lines removed and
    the rest as written.
    """
    stripped = _uncommented(text)
    try:
        tree = ast.parse(stripped)
    except (SyntaxError, ValueError):
        return stripped
    lines = stripped.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(line: int, column: int) -> int:
        # `col_offset` counts UTF-8 bytes, so it is mapped back to characters.
        head = lines[line - 1].encode("utf-8")[:column]
        return starts[line - 1] + len(head.decode("utf-8", "replace"))

    characters = list(stripped)
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if (
            isinstance(node, ast.Expr)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            end_line = value.end_lineno or value.lineno
            for index in range(
                offset(value.lineno, value.col_offset), offset(end_line, value.end_col_offset)
            ):
                if characters[index] != "\n":
                    characters[index] = " "
    return "".join(characters)


def _reads_a_variable(path: Path) -> bool:
    # The raw text and the parsed strings are both searched: a double-quoted YAML
    # scalar can spell a character as an escape, which only the parse decodes.
    text = path.read_text()
    strings = (_uncommented(text), *_strings(_parse(text)))
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
        "if: ${{ vars }}",
        "if: ${{ format('{0}', vars) == '' }}",
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


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  lint:\n    steps:\n      - name: Print env vars\n        run: env\n",
        "jobs:\n  lint:\n    timeout-minutes: 15  # vars are not read here\n"
        "    steps:\n      - run: make lint\n",
    ],
    ids=["step-name", "trailing-comment"],
)
def test_a_word_about_a_variable_is_not_a_read(tmp_path: Path, text: str) -> None:
    # A step name and a trailing comment that merely say the word read nothing,
    # so neither counts as a read of a repository variable.
    path = tmp_path / "ci.yml"
    path.write_text(text)
    assert not _reads_a_variable(path)


def _local_target(uses: str) -> str | None:
    """The repository path a local ``uses:`` names, with ``.`` and ``..`` resolved."""
    return posixpath.normpath(uses[2:]) if uses.startswith("./") else None


def test_a_local_action_lives_where_the_variable_search_reads() -> None:
    # A composite action used by path runs inside the job that uses it, so it is
    # searched like a workflow, and the search covers .github only. The path is
    # read after resolving `..`, so `./.github/../actions/x` counts as outside.
    uses = [value for path in _yaml_files() for value in _values(_parse(path.read_text()), "uses")]
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


def _api_reads(text: str) -> list[str]:
    """The API calls in *text* that return a repository variable.

    A whole-line comment and a docstring are dropped, and a line that also names
    a write verb is not a read: only a call that returns the value can stand in
    for `vars`.
    """
    found: list[str] = []
    for line in _code_only(text).splitlines():
        if API_VARIABLE_WRITE.search(line):
            continue
        found.extend(match.group(0) for match in API_VARIABLE_READ.finditer(line))
    return found


def test_no_step_or_script_reads_a_variable_through_the_api() -> None:
    # The REST endpoint, `gh variable get` and the Octokit helpers return the same
    # value as `vars`, to a step's shell or a script the required jobs run. This
    # file names those forms and is left out.
    files = _scanned_files((WORKFLOWS.parent, REPO / "scripts"))
    assert WORKFLOWS / "ci.yml" in files
    assert REPO / "scripts" / "check_issue_links.py" in files
    # A test fixture that sets a GITHUB_ variable or spells the endpoint reads
    # nothing about the pull request, so the tests directory is not searched.
    assert REPO / "tests" / "conftest.py" not in files
    hits = [path for path in files if _api_reads(path.read_text(errors="replace"))]
    assert hits == []


def test_the_api_scan_looks_only_at_source_files(tmp_path: Path) -> None:
    # A bytecode cache holds the literals of the source it was built from, so read
    # as text it matches an API read. The walk leaves compiled files and their
    # cache directory out; a source file beside them is still read.
    literal = b"gh variable get ADVISORY_PR_CHECKS"
    scripts = tmp_path / "tests" / "scripts"
    cache = scripts / "__pycache__"
    cache.mkdir(parents=True)
    compiled = cache / "test_thing.cpython-310-pytest-8.4.2.pyc"
    compiled.write_bytes(b"\x0d\x0a\x00\x00" + literal + b"\x00\x00")
    loose = scripts / "test_thing.pyc"
    loose.write_bytes(literal)
    source = scripts / "test_thing.py"
    source.write_text(f'READ = "{literal.decode()}"\n')
    assert _api_reads(compiled.read_bytes().decode("utf-8", errors="replace"))
    scanned = _scanned_files((tmp_path / "tests",))
    assert source in scanned
    assert compiled not in scanned
    assert loose not in scanned


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
    assert _api_reads(text)


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
    assert _api_reads(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "gh variable set ADVISORY_PR_CHECKS --body off",
        "gh api -X PATCH repos/o/r/actions/variables/ADVISORY_PR_CHECKS -f value=off",
        "await github.rest.actions.createRepoVariable({ owner, repo, name, value })",
        "# Use gh variable list to see the names.",
        "# Do not call gh variable get from a step.",
    ],
    ids=["gh-set", "rest-patch", "octokit-create", "prose-hint", "comment"],
)
def test_a_write_or_a_comment_is_not_an_api_read(text: str) -> None:
    # Writing a variable and mentioning the endpoint in prose change nothing about
    # what a step reads, so neither is refused.
    assert _api_reads(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "gh api --request PATCH repos/o/r/actions/variables/A -d value=off",
        "curl --request PATCH https://api.github.com/repos/o/r/actions/variables/A -d value=off",
        "gh api --method=PATCH repos/o/r/actions/variables/A -f value=off",
    ],
    ids=["gh-request", "curl-request", "method-equals"],
)
def test_a_write_spelled_with_request_or_method_is_not_an_api_read(text: str) -> None:
    # `--request` and `--method=` name the same write verb as `-X` and `--method`,
    # so a call that carries one is a write and not a read either way.
    assert _api_reads(text) == []


def _required_files(root: Path = REPO) -> list[Path]:
    """``ci.yml`` and every local action or reusable workflow it uses, however deep."""
    workflows = root / ".github" / "workflows"
    found: list[Path] = []
    pending = [workflows / "ci.yml"]
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.append(path)
        for uses in _values(_parse(path.read_text()), "uses"):
            target = _local_target(uses) if isinstance(uses, str) else None
            if target is None:
                continue
            local = root / target
            if local.suffix in (".yml", ".yaml"):
                pending.append(local)
                continue
            actions = [local / name for name in ("action.yml", "action.yaml")]
            actions = [action for action in actions if action.is_file()]
            assert actions, f"{uses} names no action.yml"
            pending.extend(actions)
    return found


PULL_REQUEST_FILTER_KEYS = ("branches", "branches-ignore", "paths", "paths-ignore")


def _pull_request_branch_filter(parsed: object) -> bool:
    # A `pull_request` trigger with a branch or path filter starts no run for
    # the pull requests it excludes, so the required check never reports on
    # them. `pull_request_target` is the same trigger with a write-scoped token.
    if not isinstance(parsed, dict):
        return False
    triggers = parsed.get("on", parsed.get(True))
    if not isinstance(triggers, dict):
        return False
    for name in ("pull_request", "pull_request_target"):
        trigger = triggers.get(name)
        if isinstance(trigger, dict) and any(key in trigger for key in PULL_REQUEST_FILTER_KEYS):
            return True
    return False


def _other_switches(text: str) -> list[str]:
    """What in a required workflow or action could act as a switch.

    A label, an event detail and a secret are each refused only as a reference:
    the word in a step name or a comment reads nothing. The raw text is searched
    beside the parsed strings, so an `env`, `run`, `runs-on` or matrix value
    counts as much as an `if`. A secret under a `with:` input is how a step
    receives one, and it is the only place within a job's steps one is allowed:
    a secret in an `env` value can gate a step the same way, so that stays
    refused. A job-level `secrets:` mapping is a keyword of a reusable-workflow
    call, which hands the secrets over rather than reading one, and passes as
    `secrets: inherit` does. A `pull_request` or `pull_request_target` trigger
    with a branch or path filter is refused outright, because it starts no run
    for the pull requests it excludes.
    """
    try:
        parsed = _parse(text)
    except _DuplicateKeyError:
        return ["duplicate mapping key"]
    raw = _uncommented(text)
    found = [
        match.group(0)
        for pattern in (LABEL, EVENT_DETAIL, ENV_DETAIL)
        for string in (raw, *_strings(parsed))
        for match in pattern.finditer(string)
    ]
    if [s for s in _strings(parsed, "with", reusable_secrets=True) if SECRET.search(s)]:
        found.append("secrets outside a with: input")
    if _pull_request_branch_filter(parsed):
        found.append("pull_request trigger filter")
    return found


def _code_switches(text: str) -> list[str]:
    # A shell script or an action's own code reads a label or an event detail
    # through the same names a workflow would. A whole-line comment and a
    # docstring name them without reading anything, so both are dropped first.
    code = _code_only(text)
    return [
        match.group(0)
        for pattern in (LABEL, EVENT_DETAIL, ENV_DETAIL)
        for match in pattern.finditer(code)
    ]


def _input_gated(text: str) -> list[str]:
    # An action or reusable workflow a required job calls reads its own inputs; a
    # condition on one of them can skip the work the caller meant to run.
    parsed = _parse(text)
    conditions = [*_values(parsed, "if"), *_values(parsed, "continue-on-error")]
    return [
        match.group(0)
        for condition in conditions
        for string in _strings(condition)
        for match in INPUT_READ.finditer(string)
    ]


def _required_switches(root: Path = REPO) -> dict[str, list[str]]:
    """What could switch a required job off, by file, across a whole checkout."""
    files = _required_files(root)
    switches = {str(path.relative_to(root)): _other_switches(path.read_text()) for path in files}
    for path in files:
        switches[str(path.relative_to(root))] += _input_gated(path.read_text())
    actions = {path.parent for path in files[1:] if path.name.startswith("action.")}
    roots = {root / "scripts"} | actions
    for directory in sorted(roots):
        if not directory.is_dir():
            continue
        for path in _scanned_files((directory,)):
            hits = _code_switches(path.read_text(errors="replace"))
            if hits:
                name = str(path.relative_to(root))
                switches[name] = switches.get(name, []) + hits
    return switches


def test_the_required_workflow_has_no_other_switch() -> None:
    files = _required_files()
    assert files[0] == WORKFLOWS / "ci.yml"
    switches = _required_switches()
    # ci.yml is in this mapping, so a condition on one of its own inputs is
    # refused as well.
    assert ".github/workflows/ci.yml" in switches
    assert switches == {name: [] for name in switches}


def test_a_required_job_cannot_gate_on_a_manual_run_input(tmp_path: Path) -> None:
    # `inputs.python-matrix` is supplied by a manual run only; a pull request
    # leaves it empty, so a condition on it starts no run there. ci.yml reads
    # that input in a matrix value and is itself scanned, so a condition on it
    # in ci.yml is refused as well.
    text = (
        "jobs:\n  test:\n    if: inputs.python-matrix == 'endpoints'\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: make lint\n"
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text)
    assert _input_gated(text)
    assert _required_switches(tmp_path)[".github/workflows/ci.yml"]


def test_a_required_job_cannot_switch_off_in_its_own_code(tmp_path: Path) -> None:
    # The `lint` job runs a shell helper and a local composite action; either can
    # read a label or the event and skip the work without the workflow saying so.
    workflows = tmp_path / ".github" / "workflows"
    action = tmp_path / ".github" / "actions" / "lint-gate"
    scripts = tmp_path / "scripts"
    for directory in (workflows, action, scripts, tmp_path / "tests"):
        directory.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: ./.github/actions/lint-gate\n"
        "      - run: scripts/ci.sh lint-python\n"
    )
    (action / "action.yml").write_text(
        "name: Gate\nruns:\n  using: composite\n  steps:\n"
        "    - shell: bash\n      run: node gate.js\n"
    )
    (action / "gate.js").write_text(
        "const skip = context.payload.pull_request.labels.some((l) => l.name === 'skip-lint');\n"
    )
    (scripts / "ci.sh").write_text(
        'jq -e \'.pull_request.labels[]?.name == "skip-lint"\' "$GITHUB_EVENT_PATH" && return 0\n'
    )
    switches = _required_switches(tmp_path)
    assert switches[".github/actions/lint-gate/gate.js"]
    assert switches["scripts/ci.sh"]


@pytest.mark.parametrize(
    "text",
    [
        '"""Release notes name github.actor for the audit log."""\n',
        '"""\nBuild the wheel; gh variable list shows what is left over.\n"""\n',
        "# GITHUB_ACTOR names the author; this line reads nothing.\n",
    ],
    ids=["docstring-actor", "docstring-api", "whole-line-comment"],
)
def test_a_whole_line_comment_or_a_docstring_is_not_a_code_switch(text: str) -> None:
    # A whole-line comment or a docstring that names an event detail or the
    # variables endpoint while saying what the code does reads nothing, so it is
    # not a switch. Prose after a value on the same line is left as written.
    assert _code_switches(text) == []
    assert _api_reads(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "jq -e '.pull_request.draft' \"$GITHUB_EVENT_PATH\"\n",
        'name = os.environ["GITHUB_ACTOR"]\n',
        "gh variable get ADVISORY_PR_CHECKS\n",
    ],
    ids=["event-path", "actor-env", "variable-get"],
)
def test_a_read_in_code_is_still_a_code_switch(text: str) -> None:
    # The control: the arms above must pass on the text, not on code that reads.
    assert _code_switches(text) or _api_reads(text)


def test_a_test_fixture_that_sets_an_event_variable_is_not_a_switch(tmp_path: Path) -> None:
    # A fixture that sets or clears a GITHUB_ variable arranges the environment
    # for a test; it reads nothing about the pull request. The scan leaves the
    # tests directory out, so such a fixture never counts.
    workflows = tmp_path / ".github" / "workflows"
    scripts = tmp_path / "scripts"
    tests = tmp_path / "tests" / "scripts"
    for directory in (workflows, scripts, tests):
        directory.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make lint\n"
    )
    (tests / "test_issue_links.py").write_text(
        "def test_x(monkeypatch, tmp_path):\n"
        '    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "e.json"))\n'
        "    for name in ('GITHUB_ACTOR', 'GITHUB_EVENT_PATH'):\n"
        "        monkeypatch.delenv(name, raising=False)\n"
    )
    switches = _required_switches(tmp_path)
    assert switches == {".github/workflows/ci.yml": []}


def test_a_script_with_a_non_utf8_byte_is_still_scanned(tmp_path: Path) -> None:
    # One byte outside UTF-8 does not make a script unreadable: it is read with
    # replacement, so a read in its code is still found.
    workflows = tmp_path / ".github" / "workflows"
    scripts = tmp_path / "scripts"
    for directory in (workflows, scripts, tmp_path / "tests"):
        directory.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: scripts/ci.sh lint\n"
    )
    (scripts / "ci.sh").write_bytes(
        b'#!/bin/bash\n# caf\xe9\njq -e ".draft" "$GITHUB_EVENT_PATH" && exit 0\n'
    )
    assert _required_switches(tmp_path)["scripts/ci.sh"]


def test_a_required_job_cannot_gate_on_an_input_secret(tmp_path: Path) -> None:
    # The caller hands a local action a token from a secret; the action skips
    # itself when the input is empty, which is what a pull request from a fork
    # supplies, so the step the job meant to run never runs.
    workflows = tmp_path / ".github" / "workflows"
    action = tmp_path / ".github" / "actions" / "lint-gate"
    for directory in (workflows, action, tmp_path / "scripts", tmp_path / "tests"):
        directory.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: ./.github/actions/lint-gate\n"
        "        with:\n          token: ${{ secrets.LINT_TOKEN }}\n"
    )
    (action / "action.yml").write_text(
        "name: Gate\ninputs:\n  token:\n    required: false\nruns:\n  using: composite\n"
        "  steps:\n    - if: ${{ inputs.token != '' }}\n      shell: bash\n      run: echo ok\n"
    )
    switches = _required_switches(tmp_path)
    assert switches[".github/actions/lint-gate/action.yml"]


def test_a_required_workflow_without_a_secret_still_passes(tmp_path: Path) -> None:
    # A secret in a `with:` input is allowed, not required: a workflow that reaches
    # no action with one has nothing to switch and must pass unchanged.
    ci = (
        (WORKFLOWS / "ci.yml")
        .read_text()
        .replace("        token: ${{ secrets.CODECOV_TOKEN }}\n", "")
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(ci)
    assert _required_switches(tmp_path) == {".github/workflows/ci.yml": []}


def test_a_repeated_mapping_key_is_refused(tmp_path: Path) -> None:
    # PyYAML keeps the last of two identical keys, so a repeated `uses:` or `if:`
    # hides the other from every scan here. Such a file is refused instead.
    text = (
        "jobs:\n  lint:\n    steps:\n"
        "      - uses: ./.github/actions/switch\n        uses: ./.github/actions/setup\n"
    )
    assert _other_switches(text)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text)
    with pytest.raises(_DuplicateKeyError):
        _required_files(tmp_path)


def test_a_pull_request_branch_filter_is_found() -> None:
    # ci.yml's comments explain why it holds no `branches` filter under its
    # `pull_request` trigger; a base branch outside main or develop gets no run.
    text = (
        "on:\n  pull_request:\n    branches: [main, develop]\n"
        "jobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make lint\n"
    )
    assert _other_switches(text)


def _pull_request_trigger(trigger: str, key: str) -> str:
    return (
        f"on:\n  {trigger}:\n    {key}: [x]\n"
        "jobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make lint\n"
    )


@pytest.mark.parametrize("trigger", ["pull_request", "pull_request_target"])
@pytest.mark.parametrize("key", ["branches", "branches-ignore", "paths", "paths-ignore"])
def test_a_pull_request_trigger_with_a_filter_is_found(trigger: str, key: str) -> None:
    # A branch or path filter starts no run for the pull requests it excludes,
    # so the required check never reports on them. `pull_request_target` is the
    # same trigger with a write-scoped token.
    assert _other_switches(_pull_request_trigger(trigger, key))


@pytest.mark.parametrize(
    "text",
    [
        "name: Gate\nruns:\n  using: composite\n  steps:\n"
        "    - if: ${{ inputs.token != '' }}\n      shell: bash\n      run: echo upload\n",
        "name: Gate\nruns:\n  using: composite\n  steps:\n"
        "    - continue-on-error: ${{ inputs.optional }}\n      shell: bash\n      run: echo upload\n",
    ],
    ids=["if", "continue-on-error"],
)
def test_an_input_gated_action_step_is_found(text: str) -> None:
    # A caller passes a secret as an input, and the callee skips itself when the
    # value is empty, which is what a pull request from a fork supplies.
    assert _input_gated(text)


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
        "jobs:\n  test:\n    steps:\n      - uses: codecov/codecov-action@v4\n"
        "        env:\n          CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}\n",
        "jobs:\n  lint:\n    steps:\n      - run: |\n"
        '          [ "$GITHUB_ACTOR" = "dependabot[bot]" ] && exit 0\n          make lint\n',
        "jobs:\n  lint:\n    steps:\n      - run: |\n"
        "          jq -e '.pull_request.draft' \"$GITHUB_EVENT_PATH\" && exit 0\n          make lint\n",
        "jobs:\n  lint:\n    steps:\n      - if: github.base_ref == 'main'\n        run: make lint\n",
        "jobs:\n  lint:\n    if: github.actor_id != 49699333\n    runs-on: ubuntu-latest\n",
        "jobs:\n  lint:\n    steps:\n"
        "      - run: jq -e '.pull_request.draft' ${{ github.event_path }} && exit 0\n",
        "jobs:\n  lint:\n    steps:\n"
        "      - if: ${{ !contains(toJSON(github), 'skip-lint') }}\n        run: make lint\n",
        "jobs:\n  lint:\n    steps:\n      - uses: actions/github-script@v7\n"
        "        with:\n          script: |\n"
        "            if (context.actor === 'dependabot[bot]') return;\n",
        "jobs:\n  lint:\n    steps:\n      - uses: actions/github-script@v7\n"
        "        with:\n          script: |\n"
        "            if (context.payload.pull_request.draft) return;\n",
        "jobs:\n  lint:\n    steps:\n      - uses: actions/github-script@v7\n"
        "        with:\n          script: |\n"
        "            if (context.payload.label.name === 'skip-lint') return;\n",
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
        "secret-in-env",
        "actor-env",
        "event-path-env",
        "base-ref-if",
        "actor-id-if",
        "event-path-expression",
        "tojson-github",
        "script-context-actor",
        "script-context-payload",
        "script-context-label",
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
        "jobs:\n  lint:\n    steps:\n      - name: Scan for leaked secrets\n"
        "        run: scripts/ci.sh lint\n",
        "jobs:\n  lint:\n    steps:\n      - name: Check release labels\n        run: echo ok\n",
        "jobs:\n  lint:\n    timeout-minutes: 15  # no secrets are read here\n"
        "    steps:\n      - run: make lint\n",
        "jobs:\n  reuse:\n    uses: ./.github/workflows/reusable.yml\n    secrets: inherit\n",
        "jobs:\n  reuse:\n    uses: ./.github/workflows/reusable.yml\n"
        "    secrets:\n      TOKEN: ${{ secrets.TOKEN }}\n",
        "on:\n  push:\n    branches: [main, develop]\n  pull_request:\n    types: [opened]\n"
        "jobs:\n  test:\n    if: always()\n",
    ],
    ids=[
        "secret-as-input",
        "event-name",
        "comment",
        "step-name-secret",
        "step-name-label",
        "trailing-comment",
        "secrets-inherit",
        "secrets-mapping",
        "push-branches",
    ],
)
def test_what_a_required_workflow_may_hold(text: str) -> None:
    assert _other_switches(text) == []


CI_GATE_HARD_GATES = [
    "lint",
    "core-install",
    "docs",
    "test",
    "frontend",
    "studio-e2e",
    "changes",
    "vscode",
    "marketplace",
]


def test_the_aggregate_check_keeps_its_hard_gates() -> None:
    # ci-gate compares each named job's result against success. Dropping a name
    # from the list makes the aggregate ignore that job, so a failure it reports
    # would not fail the check the branch protection reads. The list is pinned
    # rather than counted: a name is what matters.
    text = (WORKFLOWS / "ci.yml").read_text()
    match = re.search(r"^\s*hard_gates\s*=\s*\[(?P<body>.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert match, "ci.yml no longer names hard_gates as a list literal"
    found = re.findall(r"\"([^\"]+)\"", match.group("body"))
    assert found == CI_GATE_HARD_GATES
