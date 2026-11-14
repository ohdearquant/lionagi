"""The advisory pull-request checks can be switched off; the required check cannot.

These tests read the required workflow, the scripts and the tests it runs, and
the action code it uses, so they catch a switch added to any of them by
accident. A pull request that edits those files can also edit this test, so they
do not stop a deliberate one.

The searches here are textual, and each one keeps the limits named in its own
comment rather than chasing every input: a line that names a detail is refused
even in prose, only an environment write through a call with one of four names
is exempt, only a direct `if:` or `continue-on-error:` input condition is read,
a write blanks the whole statement that holds it, the API scan reads no file
under `tests/` and every other text file under its roots, and a file read as
bytes in a multibyte encoding can stay silent.
"""

from __future__ import annotations

import ast
import copy
import functools
import importlib.metadata
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

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
# `gh api` spells the verb `--method`, and may carry it as `--method=VERB`; `curl`
# spells it `-X` or `--request`.
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
# A label and an event detail are matched as references, so a step name that reads
# "Check release labels" is not one. A line that merely names one is still a
# refusal though: the match is textual, and this file cannot tell a reference from
# prose on a line that carries both. A step that runs `actions/github-script`
# reads the same details as `context.actor`, `context.payload.pull_request` and
# `context.payload.label`; those spellings count wherever they appear, not only
# inside a github-script block. The whole `github` context is read when it is
# passed to `toJSON`; passed to any other function, `format()` say, its details
# are not what the pattern matches.
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
# run. Only a direct `if:` or `continue-on-error:` condition is read: an indirect
# gate on an input through a step's `env:` value or through a `run:` shell test is
# outside what this pattern sees. A condition is refused as written, so a callee
# that reads an input its caller always supplies is refused too; that is a limit,
# and the condition has to move behind a step's `env:` or a `run:` test to pass.
INPUT_READ = re.compile(r"\binputs\s*(\.|\[)", re.IGNORECASE)

# A call that writes an environment variable rather than reading one. A test
# fixture sets or clears a GITHUB_ variable to arrange the environment; that names
# the variable to write it, so it reads nothing about the pull request. Only the
# four call names below are exempt: a write through another call that reaches
# os.environ (`monkeypatch.setitem`, `patch.dict`, an assigned subscript) still
# names the variable and is refused, so a test that arranges the environment with
# one has to be rewritten to `setenv` and `delenv`. A read on the same statement
# as an exempt call is blanked with it (see `_code_only`).
ENV_WRITE_CALLS = frozenset({"setenv", "delenv", "putenv", "unsetenv"})


def _is_env_write(node: ast.AST) -> bool:
    """True for a call that writes an environment variable instead of reading one."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in ENV_WRITE_CALLS


def _env_write_spans(tree: ast.AST) -> list[ast.AST]:
    """The nodes to blank for a call that writes an environment variable.

    The whole statement that holds the call is dropped, not only the call: a
    statement is the smallest span this can blank, so a read sharing it (a
    conditional expression, a loop test) is dropped too, and that is a limit.
    The iterable of the loop a variable name is drawn from is dropped as well,
    as in a clean-up loop over the names a fixture set. The call itself names
    the variable to write it, so a read in one of its value arguments, or in the
    iterable of the loop it sits in, is dropped with the blanked statement.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    spans: list[ast.AST] = []
    for node in ast.walk(tree):
        if not _is_env_write(node):
            continue
        statement: ast.AST = node
        while not isinstance(statement, ast.stmt):
            statement = parents[statement]
        spans.append(statement)
        parent = parents.get(statement)
        if isinstance(parent, ast.For):
            spans.append(parent.iter)
    return spans


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
    # Drop every line whose first non-space character is `#`. That is a comment in
    # the shell a `run:` block is handed to, and the same shape is taken as a
    # comment in every other language read here, so a `#`-first line inside a
    # heredoc, a string or a JavaScript file is dropped too. Text after a value on
    # a line stays, so the raw search is never less strict than the parse except
    # on such lines.
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _code_only(text: str) -> str:
    """*text* with whole-line comments, bare string statements and env writes out.

    Only code can read a label or an event: a `#` line and a bare string
    statement (a module, class or function docstring, and any other string
    standing alone as a statement) say what the code is for, and a call that
    sets or clears an environment variable names the variable to write it rather
    than to read it. A Python file is read through `ast`, so all three are
    dropped; text that does not parse as Python keeps its `#` lines removed and
    the rest as written. A `#`-first line is dropped wherever it sits, so one
    inside a shell string is dropped too, and that is a limit.
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

    def blank(node: ast.AST) -> None:
        for index in range(
            offset(node.lineno, node.col_offset),
            offset(node.end_lineno or node.lineno, node.end_col_offset),
        ):
            if characters[index] != "\n":
                characters[index] = " "

    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if (
            isinstance(node, ast.Expr)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            blank(value)
    for node in _env_write_spans(tree):
        blank(node)
    return "".join(characters)


def _reads_a_variable(path: Path) -> bool:
    # The raw text and the parsed strings are both searched: a double-quoted YAML
    # scalar can spell a character as an escape, which only the parse decodes.
    # `vars` is matched as a reference, so a property that merely ends in `vars`
    # (`steps.x.outputs.vars`) counts, and a file that does not decode as UTF-8
    # (a UTF-16 script) is read with replacement and can stay silent. Both are
    # accepted limits.
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

    A whole-line comment, a bare string statement and a call that writes an
    environment variable are dropped. A line that names a write verb is not a
    read: only a call that returns the value can stand in for `vars`. Three
    limits are kept: a write verb hides any read beside it on the same line; only
    the write spellings above are recognised, so an Octokit `request` string that
    names the endpoint and an implicit `gh api -f` are refused even though they
    write; and every text file under the roots is read, so a fixture that merely
    holds the endpoint (a JSON sample) is refused as well.
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
    # Limit: this scan reads `.github` and `scripts` only, so an endpoint reached
    # from a file under `tests/` is out of it.
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


# The one event detail a required job may read: the test matrix runs the two
# ends of the supported set on a pull request into dev/lionagi and the full set
# on every other base, so the pull request that promotes dev/lionagi into main
# still runs every version before main moves. It is matched as this exact text
# and removed before the scan, so any edit to it is refused here and has to be
# argued again; its selections are pinned by the matrix tests below.
SANCTIONED_TEST_MATRIX = "${{ fromJSON((github.event_name == 'workflow_dispatch' && inputs.python-matrix == 'endpoints') && '[\"3.10\",\"3.14\"]' || (github.event_name == 'pull_request' && needs.changes.outputs.code == 'false' && github.base_ref == 'dev/lionagi') && '[\"3.10\"]' || (github.event_name == 'pull_request' && github.base_ref == 'dev/lionagi') && '[\"3.10\",\"3.14\"]' || '[\"3.10\",\"3.11\",\"3.12\",\"3.13\",\"3.14\"]') }}"


def _sanctioned_matrix_removed(parsed: object) -> object:
    """The parsed workflow with the test job's sanctioned matrix value blanked.

    Only that one location is exempt: the same text anywhere else, a job's
    `if:` or another job's matrix, stays in the scan and is refused.
    """
    try:
        matrix = parsed["jobs"]["test"]["strategy"]["matrix"]  # type: ignore[index]
    except (KeyError, TypeError):
        return parsed
    if not isinstance(matrix, dict) or matrix.get("python-version") != SANCTIONED_TEST_MATRIX:
        return parsed
    parsed = copy.deepcopy(parsed)
    parsed["jobs"]["test"]["strategy"]["matrix"]["python-version"] = ""  # type: ignore[index]
    return parsed


def _other_switches(text: str) -> list[str]:
    """What in a required workflow or action could act as a switch.

    A label, an event detail and a secret are each refused only as a reference:
    the word in a step name reads nothing, and a whole-line comment is dropped,
    but a reference after a value on the same line is still refused, since only
    whole-line comments come out. The raw text is searched beside the parsed
    strings, so an `env`, `run`, `runs-on` or matrix value counts as much as an
    `if`. A secret under a `with:` input is how a step
    receives one, and it is the only place within a job's steps one is allowed:
    a secret in an `env` value can gate a step the same way, so that stays
    refused. A job-level `secrets:` mapping is a keyword of a reusable-workflow
    call, which hands the secrets over rather than reading one, and passes as
    `secrets: inherit` does. A `pull_request` or `pull_request_target` trigger
    with a branch or path filter is refused outright, because it starts no run
    for the pull requests it excludes.

    One event detail is exempt: `SANCTIONED_TEST_MATRIX` as the test job's
    `strategy.matrix.python-version`, and only there. The parsed scan blanks that
    one value; the raw-text scan drops the expression only where it follows a
    `python-version:` key, and a copy under another job's matrix is still found
    by the parsed scan.
    """
    try:
        parsed = _parse(text)
    except _DuplicateKeyError:
        return ["duplicate mapping key"]
    raw = _uncommented(text).replace(
        f"python-version: {SANCTIONED_TEST_MATRIX}", "python-version: "
    )
    found = [
        match.group(0)
        for pattern in (LABEL, EVENT_DETAIL, ENV_DETAIL)
        for string in (raw, *_strings(_sanctioned_matrix_removed(parsed)))
        for match in pattern.finditer(string)
    ]
    if [s for s in _strings(parsed, "with", reusable_secrets=True) if SECRET.search(s)]:
        found.append("secrets outside a with: input")
    if _pull_request_branch_filter(parsed):
        found.append("pull_request trigger filter")
    return found


def _code_switches(text: str) -> list[str]:
    # A shell script or an action's own code reads a label or an event detail
    # through the same names a workflow would. A whole-line comment, a docstring
    # and a call that writes an environment variable name them without reading
    # anything, so all three are dropped first.
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
    roots = {root / "scripts", root / "tests"} | actions
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


def _test_matrix_versions(
    expression: str, *, event: str, base: str = "", code: str = "", matrix: str = ""
) -> list[str]:
    """Evaluate the test job's python-version expression for one event.

    GitHub's `a && b || c` takes the same branches as Python's `a and b or c` here,
    because every value the expression selects is a non-empty string.
    """
    inner = expression.removeprefix("${{").removesuffix("}}").strip()
    names = {
        "github.event_name": "EVENT",
        "github.base_ref": "BASE",
        "needs.changes.outputs.code": "CODE",
        "inputs.python-matrix": "MATRIX",
        "fromJSON": "FROM_JSON",
        "&&": " and ",
        "||": " or ",
    }
    for spelling, name in names.items():
        inner = inner.replace(spelling, name)
    scope = {"EVENT": event, "BASE": base, "CODE": code, "MATRIX": matrix, "FROM_JSON": json.loads}
    return eval(inner, {"__builtins__": {}}, scope)  # noqa: S307 - the pinned text above


FULL_SET = ["3.10", "3.11", "3.12", "3.13", "3.14"]


def test_the_test_matrix_reads_the_sanctioned_expression() -> None:
    matrix = _load("ci.yml")["jobs"]["test"]["strategy"]["matrix"]
    assert matrix["python-version"] == SANCTIONED_TEST_MATRIX


@pytest.mark.parametrize(
    ("event", "base", "code", "matrix", "expected"),
    [
        ("pull_request", "dev/lionagi", "true", "", ["3.10", "3.14"]),
        ("pull_request", "main", "true", "", FULL_SET),
        ("pull_request", "release/x", "true", "", FULL_SET),
        ("pull_request", "dev/lionagi", "false", "", ["3.10"]),
        ("pull_request", "main", "false", "", FULL_SET),
        ("pull_request", "release/x", "false", "", FULL_SET),
        # `changes` failed or was skipped: its output is empty.
        ("pull_request", "dev/lionagi", "", "", ["3.10", "3.14"]),
        ("pull_request", "main", "", "", FULL_SET),
        ("push", "", "true", "", FULL_SET),
        ("push", "", "false", "", FULL_SET),
        ("workflow_dispatch", "", "", "full", FULL_SET),
        ("workflow_dispatch", "", "", "endpoints", ["3.10", "3.14"]),
    ],
)
def test_the_test_matrix_selection(event, base, code, matrix, expected) -> None:
    versions = _test_matrix_versions(
        SANCTIONED_TEST_MATRIX, event=event, base=base, code=code, matrix=matrix
    )
    assert versions == expected


def test_a_base_ref_read_beside_the_sanctioned_matrix_is_still_refused() -> None:
    text = _ci_gate_text()
    assert _other_switches(text) == []
    condition = (
        "    if: ${{ !cancelled() }}\n    runs-on: ubuntu-24.04\n    # Every job in this workflow"
    )
    assert text.count(condition) == 1, "the test job header in ci.yml changed; update this fixture"
    edited = text.replace(
        condition, condition.replace("!cancelled()", "!cancelled() && github.base_ref != ''"), 1
    )
    assert "github.base_ref" in _other_switches(edited)


def test_the_sanctioned_matrix_text_is_refused_anywhere_but_the_test_matrix() -> None:
    text = _ci_gate_text()
    condition = (
        "    if: ${{ !cancelled() }}\n    runs-on: ubuntu-24.04\n    # Every job in this workflow"
    )
    assert text.count(condition) == 1, "the test job header in ci.yml changed; update this fixture"
    as_condition = text.replace(
        condition, condition.replace("${{ !cancelled() }}", SANCTIONED_TEST_MATRIX), 1
    )
    assert "github.base_ref" in _other_switches(as_condition)
    as_flag = text.replace(
        condition, condition + f"\n    continue-on-error: {SANCTIONED_TEST_MATRIX}", 1
    )
    assert "github.base_ref" in _other_switches(as_flag)


def test_an_edited_matrix_expression_is_refused() -> None:
    text = _ci_gate_text()
    widened = SANCTIONED_TEST_MATRIX.replace("'dev/lionagi'", "'main'")
    assert widened != SANCTIONED_TEST_MATRIX
    edited = text.replace(SANCTIONED_TEST_MATRIX, widened, 1)
    assert "github.base_ref" in _other_switches(edited)


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
        # The docstring sits after multibyte characters on the same line, so its
        # span only lines up once the byte offsets `ast` reports are mapped back to
        # characters. A read after it must survive the blanking.
        "\u00e9" * 21 + ' = 1; """doc"""; z = os.environ["GITHUB_ACTOR"]\n',
    ],
    ids=["event-path", "actor-env", "variable-get", "multibyte-docstring"],
)
def test_a_read_in_code_is_still_a_code_switch(text: str) -> None:
    # The control: the arms above must pass on the text, not on code that reads.
    assert _code_switches(text) or _api_reads(text)


def test_a_test_fixture_that_sets_an_event_variable_is_not_a_switch(tmp_path: Path) -> None:
    # A fixture that sets or clears a GITHUB_ variable arranges the environment
    # for a test; it reads nothing about the pull request, but the tests directory
    # is searched like the scripts, so the write has to be exempted rather than the
    # directory left out. Clearing a whole run of names counts as one.
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


def test_a_read_in_a_test_is_still_a_switch(tmp_path: Path) -> None:
    # The tests directory is searched, so a test the required job runs that reads
    # the event instead of writing it is refused like any other code.
    workflows = tmp_path / ".github" / "workflows"
    tests = tmp_path / "tests" / "scripts"
    for directory in (workflows, tests, tmp_path / "scripts"):
        directory.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: scripts/ci.sh test-python\n"
    )
    (tests / "test_issue_links.py").write_text(
        "import os\n"
        "def test_x():\n"
        '    if os.environ.get("GITHUB_EVENT_PATH") == "draft":\n'
        "        return\n"
    )
    switches = _required_switches(tmp_path)
    assert switches[".github/workflows/ci.yml"] == []
    assert switches["tests/scripts/test_issue_links.py"]


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
    expected = {".github/workflows/ci.yml": []}
    for source in _required_files()[1:]:
        relative = source.relative_to(REPO)
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        expected[relative.as_posix()] = []
    assert _required_switches(tmp_path) == expected


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

# A conditional job is allowed to skip only when the path-filter output that gates
# it reports its inputs unchanged; the flag it is read against is pinned with it.
CI_GATE_CONDITIONAL_GATES = {
    "studio-docker": "studio",
    "minimal-import": "imports",
    "no-eager-heavies": "imports",
    "compat-deps": "compat",
    "lionrs": "lionrs",
    "lion1": "lion1",
}

# The aggregate job's own keys, its one step's keys, and the path filters the
# `changes` job parses. Each is the value the file holds now; a deliberate change
# edits the pin beside the edit. A key added to the job (`if:` reading the event
# name, `continue-on-error: true`) can make the required check report success
# without the aggregate judging anything, and a path dropped from a filter can
# let a conditional job skip on the pull requests that change it. What runs inside
# a required job (a step's `run:`, `if:` or `continue-on-error`) is not pinned
# here: these pins cover the aggregate job, the jobs it needs, and the filters and
# outputs that let a conditional job skip.
CI_GATE_JOB_KEYS = {"if": "${{ always() }}", "runs-on": "ubuntu-latest", "timeout-minutes": 10}
CI_GATE_STEP_KEYS = frozenset({"name", "env", "run"})
CI_CHANGES_FILTERS = {
    "studio": [
        "apps/studio/**",
        "lionagi/**",
        "lionhub/**",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "marketplace/**",
        ".claude-plugin/**",
        ".dockerignore",
        ".github/workflows/ci.yml",
    ],
    "adr": ["docs/adr/**", "lionagi/state/lifecycle/**"],
    "imports": [
        "lionagi/**",
        "lionhub/**",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
    ],
    # The `lionagi/` entries of `compat` are not listed here: they are recomputed from
    # the imports in the files the job runs (see the compat filter test further down).
    "compat": [
        "pyproject.toml",
        "uv.lock",
        # Hub code the compatibility tests import, listed by hand: only lionagi imports
        # are derived.
        "lionhub/src/lionhub/serve/cli/**",
        "tests/conftest.py",
        "tests/_shared_conftest.py",
        "tests/state/conftest.py",
        "tests/state/test_compatibility_*.py",
        ".github/workflows/ci.yml",
    ],
    "lionrs": ["lionrs/**", ".github/workflows/ci.yml"],
    "lion1": ["lion1/**", ".github/workflows/ci.yml"],
}
CI_CHANGES_OUTPUTS = {
    **{name: f"${{{{ steps.filter.outputs.{name} }}}}" for name in CI_CHANGES_FILTERS},
    # From its own step: true when any changed file is outside the documentation paths.
    "code": "${{ steps.code.outputs.code }}",
}

# Anchored at the start of the `run:` text and at its end: the heredoc has to feed
# it alone, so a command before `python - <<'PY'` (an `exit 0`) or in place of it
# (an `echo`) is not skipped past the match.
CI_GATE_HEREDOC = re.compile(r"\Apython - <<'PY'\n(?P<body>.*?)\n\s*PY\s*\Z", re.DOTALL)


def _ci_gate_source() -> ast.Module:
    """The Python the aggregate check runs, parsed from its ``run:`` block."""
    run = _load("ci.yml")["jobs"]["ci-gate"]["steps"][0]["run"]
    match = CI_GATE_HEREDOC.search(run)
    assert match, "the aggregate check no longer runs a Python heredoc"
    return ast.parse(textwrap.dedent(match.group("body")))


def _ci_gate_text() -> str:
    return (WORKFLOWS / "ci.yml").read_text()


def _changes_filters(workflows: Path = WORKFLOWS) -> dict:
    """The path filters the `changes` job parses, a repeated key refused as its action refuses it."""
    steps = _parse((workflows / "ci.yml").read_text())["jobs"]["changes"]["steps"]
    filter_step = next(step for step in steps if step.get("id") == "filter")
    return _parse(filter_step["with"]["filters"])


def _pinned_changes_filters(workflows: Path = WORKFLOWS) -> dict:
    """The filters as CI_CHANGES_FILTERS pins them: `compat` without its derived `lionagi/` entries."""
    filters = _changes_filters(workflows)
    filters["compat"] = [path for path in filters["compat"] if not path.startswith("lionagi/")]
    return filters


def _ci_gate_body(text: str) -> str:
    """The heredoc body in *text*, refusing a `run:` the heredoc does not fill."""
    run = _parse(text)["jobs"]["ci-gate"]["steps"][0]["run"]
    match = CI_GATE_HEREDOC.search(run)
    assert match, "the aggregate check no longer runs a Python heredoc"
    return match.group("body")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "      run: |\n        python - <<'PY'\n",
            "      run: |\n        exit 0\n        python - <<'PY'\n",
            id="command-before-heredoc",
        ),
        pytest.param(
            "      run: |\n        python - <<'PY'\n",
            "      run: |\n        echo python - <<'PY'\n",
            id="other-command-before-heredoc",
        ),
    ],
)
def test_the_aggregate_check_reads_only_its_heredoc(old: str, new: str) -> None:
    # The step reports python's exit status only while the heredoc is the command
    # that runs. `exit 0` before it passes the step whatever the gate says, and
    # `echo` in its place never runs the gate; both leave the script text intact,
    # so the match is anchored at the start and end of the `run:` value.
    text = _ci_gate_text()
    assert _ci_gate_body(text)
    mutated = text.replace(old, new, 1)
    assert mutated != text
    with pytest.raises(AssertionError):
        _ci_gate_body(mutated)


def test_the_changes_job_keeps_its_path_filters() -> None:
    # Each filter decides whether a conditional job runs or skips. Dropping a path
    # from one makes that job report its inputs unchanged on the pull requests that
    # change only that path, so the job skips and the aggregate accepts the skip;
    # the required check then passes with the job never run on its own paths. The
    # `lionagi/` entries of `compat` are compared by the test that derives them.
    assert _pinned_changes_filters() == CI_CHANGES_FILTERS


def test_the_changes_job_keeps_its_outputs() -> None:
    # The aggregate reads each conditional job's flag from these outputs. An output
    # written as a constant (`lion1: 'false'`) reports the inputs unchanged on every
    # pull request, so the job skips and the skip is accepted.
    assert _parse(_ci_gate_text())["jobs"]["changes"]["outputs"] == CI_CHANGES_OUTPUTS


def test_the_changes_job_outputs_pin_refuses_a_constant() -> None:
    old = "      lion1: ${{ steps.filter.outputs.lion1 }}\n"
    assert _ci_gate_text().count(old) == 1
    text = _ci_gate_text().replace(old, "      lion1: 'false'\n", 1)
    assert _parse(text)["jobs"]["changes"]["outputs"] != CI_CHANGES_OUTPUTS


def test_a_repeated_filter_name_is_refused(tmp_path: Path) -> None:
    # The filters are a YAML document of their own inside the step; a second
    # `lion1:` key would silently replace the first under a lenient parser.
    old = "          lion1:\n"
    assert _ci_gate_text().count(old) == 1
    text = _ci_gate_text().replace(
        old, "          lion1:\n            - 'docs/**'\n          lion1:\n", 1
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text)
    with pytest.raises(_DuplicateKeyError):
        _changes_filters(workflows)


@pytest.mark.parametrize(
    "path",
    [
        "            - 'lion1/**'\n",
        "            - 'apps/studio/**'\n",
        "            - 'uv.lock'\n",
        "            - 'docs/adr/**'\n",
        "            - 'lionrs/**'\n",
        "            - 'tests/state/conftest.py'\n",
    ],
    ids=["lion1", "studio", "imports", "adr", "lionrs", "compat"],
)
def test_the_changes_job_pin_refuses_a_dropped_path(path: str, tmp_path: Path) -> None:
    # One arm per filter, so dropping a path from any of them is caught: a path
    # shared by several filters would hide a drop from one alone.
    text = _ci_gate_text().replace(path, "", 1)
    assert text != _ci_gate_text()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text)
    assert _pinned_changes_filters(workflows) != CI_CHANGES_FILTERS


def _ci_gate_job_is_pinned(workflows: Path = WORKFLOWS) -> None:
    """Refuse any edit to the aggregate job's own keys, the jobs it needs, its steps or first step."""
    job = _parse((workflows / "ci.yml").read_text())["jobs"]["ci-gate"]
    assert {k: v for k, v in job.items() if k not in ("needs", "steps")} == CI_GATE_JOB_KEYS
    assert sorted(job["needs"]) == sorted(CI_GATE_HARD_GATES + list(CI_GATE_CONDITIONAL_GATES))
    assert len(job["steps"]) == 1
    assert set(job["steps"][0]) == CI_GATE_STEP_KEYS


def test_the_aggregate_job_keeps_its_own_keys() -> None:
    # The gate it runs is decided by more than the script inside it. A job `if:`
    # that reads the event name skips the aggregate on pull requests, leaving a
    # skipped required check; `continue-on-error` at either level, or a step `if`,
    # passes the job while its command does not decide the result. The job's keys
    # other than `needs` and `steps`, its one step, and that step's keys are each
    # pinned exactly, so an edit to any of them fails here.
    _ci_gate_job_is_pinned()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "    if: ${{ always() }}\n",
            "    if: ${{ always() && github.event_name != 'pull_request' }}\n",
            id="job-if-event-name",
        ),
        pytest.param(
            "    runs-on: ubuntu-latest\n    steps:\n",
            "    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n",
            id="job-continue-on-error",
        ),
        pytest.param(
            "        NEEDS_CONTEXT: ${{ toJSON(needs) }}\n",
            "        NEEDS_CONTEXT: ${{ toJSON(needs) }}\n      continue-on-error: true\n",
            id="step-continue-on-error",
        ),
        pytest.param(
            "      run: |\n",
            "      if: github.event_name == 'push'\n      run: |\n",
            id="step-if-event-name",
        ),
        pytest.param(
            " docs, test, frontend,",
            " docs, frontend,",
            id="needs-drops-a-hard-gate",
        ),
    ],
)
def test_the_aggregate_job_pin_refuses_an_edit(old: str, new: str, tmp_path: Path) -> None:
    # Each edit above leaves the gate's script untouched and would otherwise be
    # invisible here: the script answers with the same exit code while the job
    # above it stops reading that answer, or skips entirely.
    text = _ci_gate_text()
    job = text.split("  ci-gate:\n", 1)[1].split("  publish:\n", 1)[0]
    assert job.count(old) == 1, old
    edited_job = job.replace(old, new, 1)
    text = text.replace(job, edited_job, 1)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text)
    with pytest.raises(AssertionError):
        _ci_gate_job_is_pinned(workflows)


def _needs_context(results: dict[str, str], outputs: dict[str, str] | None) -> dict:
    """A ``toJSON(needs)`` value with every job the aggregate check reads.

    With *outputs* None the ``changes`` job reports no outputs at all, which is
    how a job that never set the flag reaches the aggregate.
    """
    needs = {name: {"result": results.get(name, "success")} for name in CI_GATE_HARD_GATES}
    for name in CI_GATE_CONDITIONAL_GATES:
        needs.setdefault(name, {"result": results.get(name, "success")})
    needs["changes"] = {
        "result": results.get("changes", "success"),
        "outputs": (
            {}
            if outputs is None
            else {flag: outputs.get(flag, "false") for flag in CI_GATE_CONDITIONAL_GATES.values()}
        ),
    }
    return needs


def _exec_ci_gate(needs: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run the aggregate check on *needs* and return the names it ends with."""
    monkeypatch.setenv("NEEDS_CONTEXT", json.dumps(needs))
    namespace: dict = {"__name__": "__main__"}
    try:
        exec(compile(_ci_gate_source(), "ci-gate", "exec"), namespace)
    except SystemExit:
        pass
    return namespace


def _run_ci_gate(needs: dict, monkeypatch: pytest.MonkeyPatch, capsys) -> tuple[int, str]:
    monkeypatch.setenv("NEEDS_CONTEXT", json.dumps(needs))
    code = compile(_ci_gate_source(), "ci-gate", "exec")
    try:
        exec(code, {"__name__": "__main__"})
    except SystemExit as exit:
        return int(exit.code or 0), capsys.readouterr().out
    return 0, capsys.readouterr().out


def test_the_aggregate_check_keeps_its_hard_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    # ci-gate compares each named job's result against success. Dropping a name
    # from the list makes the aggregate ignore that job, so a failure it reports
    # would not fail the check the branch protection reads. The value the check
    # ends with is compared, not the literal it starts from: a statement after the
    # list that edits it (`remove`, an append) is caught too. Narrowing the loop
    # instead of the list is what the failure case over every name below catches.
    assert _exec_ci_gate(_needs_context({}, {}), monkeypatch)["hard_gates"] == CI_GATE_HARD_GATES


def test_the_aggregate_check_keeps_its_conditional_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each conditional job is pinned with the flag it may skip against. Dropping an
    # entry makes the aggregate stop reading that job, so a skip of it would no
    # longer be checked against the path filter that is supposed to allow it.
    assert (
        _exec_ci_gate(_needs_context({}, {}), monkeypatch)["conditional_gates"]
        == CI_GATE_CONDITIONAL_GATES
    )


@pytest.mark.parametrize("name", CI_GATE_HARD_GATES)
@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_no_hard_gate_may_fail_skip_or_cancel_the_aggregate(
    name: str, result: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # One arm per name and result, so a change that stops judging a particular
    # gate or a particular result (a slice in the loop, a rename in the list) is
    # caught for the gate and the result it drops and not only for the first. A
    # skipped or cancelled required job reports neither success nor failure, so
    # it has to fail the aggregate like a failed one does.
    rc, out = _run_ci_gate(_needs_context({name: result}, {}), monkeypatch, capsys)
    assert rc == 1, out


@pytest.mark.parametrize(
    ("results", "outputs", "expected"),
    [
        pytest.param({}, {}, 0, id="all-success"),
        pytest.param({"lion1": "skipped"}, {"lion1": "true"}, 1, id="conditional-changed-skip"),
        pytest.param({"lion1": "cancelled"}, {"lion1": "true"}, 1, id="conditional-changed-cancel"),
        pytest.param({"lion1": "failure"}, {"lion1": "true"}, 1, id="conditional-changed-failure"),
        pytest.param(
            {"lion1": "failure"}, {"lion1": "false"}, 1, id="conditional-unchanged-failure"
        ),
        pytest.param({}, None, 1, id="changes-output-missing"),
    ],
)
def test_the_aggregate_check_fails_on_a_bad_result(
    results: dict[str, str],
    outputs: dict[str, str] | None,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    # A hard gate must succeed, and a conditional job may skip only when its inputs
    # did not change and the flag that says so was reported. Each arm carries the
    # exit code expected of it, so a comparison cannot pass a case that asserts only
    # "the run finished": inverting one turns its arms red.
    rc, out = _run_ci_gate(_needs_context(results, outputs), monkeypatch, capsys)
    assert rc == expected, out


@pytest.mark.parametrize("job", sorted(CI_GATE_CONDITIONAL_GATES))
@pytest.mark.parametrize(("result", "expected"), [("skipped", 0), ("cancelled", 1)])
def test_a_conditional_job_may_skip_an_unchanged_run_but_not_cancel_one(
    job: str, result: str, expected: int, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # When the paths a conditional job filters on did not change it skips, which is
    # the ordinary path on most pull requests, so that skip must pass. A cancelled
    # job never ran, so it fails there the same way it fails a changed run.
    outputs = {CI_GATE_CONDITIONAL_GATES[job]: "false"}
    rc, out = _run_ci_gate(_needs_context({job: result}, outputs), monkeypatch, capsys)
    assert rc == expected, out


@pytest.mark.parametrize("job", sorted(CI_GATE_CONDITIONAL_GATES))
@pytest.mark.parametrize("result", ["cancelled", "failure"])
def test_a_conditional_job_that_is_not_success_fails_a_changed_run(
    job: str, result: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # The inputs a conditional job filters on changed, so the job has to run and
    # succeed: a cancelled or failed job means the check the merge depends on saw
    # nothing, so the aggregate fails. The unchanged arms above cover the other
    # direction, so a comparison that accepts either result is caught here.
    outputs = {CI_GATE_CONDITIONAL_GATES[job]: "true"}
    rc, out = _run_ci_gate(_needs_context({job: result}, outputs), monkeypatch, capsys)
    assert rc == 1, out


# The compat-deps job swaps the locked aiosqlite and SQLAlchemy for a matrix row's
# versions and runs the compatibility tests on them. `_compat_deps_problems` reads
# that job from the workflow, so a matrix row can be added or removed without
# touching it.
#
# The text that decides whether the right tests run on the right versions is pinned
# exactly: this file holds a copy of it, and changing it in ci.yml means changing the
# copy here in the same commit. That refuses harmless edits as well, such as another
# pytest option, or another version check that also compares both versions exactly.
# Those edits have to touch this file too, which is the cost of an exact pin.
#
# Pinned exactly:
#   - After the step named "Replace aiosqlite and SQLAlchemy with the matrix versions"
#     there are exactly two steps, in this order: COMPAT_ASSERT_STEP, then
#     COMPAT_PYTEST_STEP. Each step's `name` equals its constant, and its `run`, as
#     YAML parses the value (so a block scalar and a quoted scalar with `\n` escapes
#     that hold the same text are the same), equals COMPAT_ASSERT_RUN or
#     COMPAT_PYTEST_RUN byte for byte. The only other key a step may have is
#     `timeout-minutes`, which must then be a positive integer.
#   - The job's `env` is exactly COMPAT_JOB_ENV: UV_PYTHON, EXPECTED_AIOSQLITE and
#     EXPECTED_SQLALCHEMY, each taken from the matrix row.
#   - The job's keys are among COMPAT_JOB_KEYS, so `container`, `services`, `defaults`,
#     `continue-on-error` and any other key are refused. `strategy` holds only
#     `fail-fast` and `matrix`, and `matrix` only `include`. The lowest SQLAlchemy and
#     the lowest aiosqlite in the rows are the `>=` floors that pyproject.toml declares.
#
# Checked on every step of the job, the steps before the replacement included: no
# `continue-on-error`, `if`, `shell` or `working-directory`, no step `env` that sets an
# expected version, and no `run` text that mentions GITHUB_ENV.
#
# The version check is also run, in a test of its own. With the installed versions as the
# expected ones, COMPAT_ASSERT_SCRIPT must exit 0. For each package it must exit non-zero
# without a Python traceback when the expected version is any of five: 0.0.0, 999999.0,
# the installed version with its last number raised by one, the installed version with
# `.1` appended, or the installed version with its last number removed (0.22.1 becomes
# 0.22). That tests the copy in this file, so it stays correct when someone edits it.
#
# Not read: the `run` text of the steps before the replacement and of the replacement
# step (only their switches and GITHUB_ENV mention are read, as above); a write to
# GITHUB_PATH in those steps (the uv install step writes one, and a later step's `uv`
# resolves through PATH); the `with:` inputs of a `uses:` step; what a script file or an
# action that a step calls does; and workflow-level `env:` and `defaults:`.
COMPAT_AFTER_REPLACE_KEYS = {"name", "run", "timeout-minutes"}
# Version strings handed to the assertion script as the expected ones, besides the
# installed versions and three derived from them. 0.0.0 sorts below and 999999.0 above
# every release as strings, so a check that compares with only `<` or only `>` passes
# one of them. A string such as `not-1.0` sorts above every digit, which would hide a
# `<` check.
COMPAT_BELOW = "0.0.0"
COMPAT_ABOVE = "999999.0"
COMPAT_EXPECTED_VARIABLES = {"EXPECTED_AIOSQLITE": "aiosqlite", "EXPECTED_SQLALCHEMY": "sqlalchemy"}
COMPAT_LOCK_STEP = "Install dependencies from the lock"
COMPAT_REPLACE_STEP = "Replace aiosqlite and SQLAlchemy with the matrix versions"
COMPAT_ASSERT_STEP = "Assert the interpreter has the matrix versions"
COMPAT_PYTEST_STEP = "Run the compatibility-mode tests"


def _compat_run(script: str) -> str:
    """The `run` text that hands *script* to `python -` through a quoted heredoc."""
    return f"uv run --no-sync python - <<'PY'\n{script}PY\n"


# The script of the assertion step, and the two `run` texts the pin requires. The
# script is a constant of its own so that a test can run it, and the step's `run` is
# built from it; it may not hold a line that is exactly `PY`, which would end the
# heredoc early in the shell.
COMPAT_ASSERT_SCRIPT = """\
import os
from importlib.metadata import version

expected = {
    "aiosqlite": os.environ["EXPECTED_AIOSQLITE"],
    "sqlalchemy": os.environ["EXPECTED_SQLALCHEMY"],
}
mismatched = []
for name, wanted in expected.items():
    found = version(name)
    print(f"{name}: installed {found}, matrix {wanted}")
    if found != wanted:
        mismatched.append(f"{name} {found} (matrix {wanted})")
if mismatched:
    raise SystemExit("not the matrix versions: " + ", ".join(mismatched))
"""
COMPAT_ASSERT_RUN = _compat_run(COMPAT_ASSERT_SCRIPT)
COMPAT_PYTEST_RUN = (
    "uv run --no-sync pytest -n 0 --maxfail=0 "
    '-m "not performance and not flaky_quarantine" '
    "--disable-warnings tests/state/test_compatibility_*.py"
)
# The steps after the replacement: (name, the constant's name for messages, its `run` text).
COMPAT_PINNED_STEPS = [
    (COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN", COMPAT_ASSERT_RUN),
    (COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN", COMPAT_PYTEST_RUN),
]
COMPAT_JOB_ENV = {
    "UV_PYTHON": "${{ matrix.python-version }}",
    "EXPECTED_AIOSQLITE": "${{ matrix.aiosqlite }}",
    "EXPECTED_SQLALCHEMY": "${{ matrix.sqlalchemy }}",
}
COMPAT_JOB_KEYS = {"name", "needs", "if", "runs-on", "timeout-minutes", "strategy", "env", "steps"}
COMPAT_STRATEGY_KEYS = {"fail-fast", "matrix"}


def _compat_step_label(step: dict) -> str:
    return repr(step.get("name") or step.get("uses") or "an unnamed step")


def _compat_declared_floor(
    pyproject: Path, package: str, extra: str | None
) -> tuple[str, Version | None]:
    """The core requirement for *package* in pyproject.toml, and the `>=` floor it declares."""
    for text in tomllib.loads(pyproject.read_text())["project"]["dependencies"]:
        requirement = Requirement(text)
        if canonicalize_name(requirement.name) != package:
            continue
        if extra is not None and extra not in requirement.extras:
            continue
        floors = [Version(spec.version) for spec in requirement.specifier if spec.operator == ">="]
        return text, (max(floors) if floors else None)
    return package, None


@functools.cache
def _compat_run_check(body: str, aiosqlite: str, sqlalchemy: str) -> tuple[int, str]:
    """Run a version-check script on this interpreter, with the two expected versions in its env."""
    env = {**os.environ, "EXPECTED_AIOSQLITE": aiosqlite, "EXPECTED_SQLALCHEMY": sqlalchemy}
    result = subprocess.run(
        [sys.executable, "-I", "-"],
        input=body,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stderr.strip()


def _compat_raise_last_number(version: str) -> str:
    """*version* with its last run of digits raised by one: 0.22.1 becomes 0.22.2."""
    return re.sub(r"\d+(?=\D*$)", lambda match: str(int(match[0]) + 1), version, count=1)


def _compat_drop_last_number(version: str) -> str | None:
    """*version* without its last dot-separated number: 0.22.1 becomes 0.22.

    None when *version* has a single number, because nothing would be left to compare with.
    """
    shorter, dot, _ = version.rpartition(".")
    return shorter if dot else None


def _compat_probe_problems(script: str) -> list[str]:
    """Why *script* cannot be relied on to stop a job on a version other than the expected one.

    The script is run on this interpreter. It must exit 0 when the expected versions are
    the installed ones. For each package, it must then exit non-zero, and without a Python
    traceback, when that package's expected version is any of five versions that differ
    from the installed one: one below every release, one above every release, the installed
    version with its last number raised by one, the installed version with `.1` appended,
    and the installed version with its last number removed. The last one catches a check
    that accepts an expected version which the installed version starts with. When the
    installed version has a single number that probe cannot be built, and that is reported.
    """
    installed = {name: importlib.metadata.version(name) for name in ("aiosqlite", "sqlalchemy")}
    code, error = _compat_run_check(script, installed["aiosqlite"], installed["sqlalchemy"])
    if code != 0:
        return [
            f"the script exited {code} when the expected versions were the installed ones: {error}"
        ]
    problems = []
    for variable, package in COMPAT_EXPECTED_VARIABLES.items():
        probes = [
            (COMPAT_BELOW, "a version below every release"),
            (COMPAT_ABOVE, "a version above every release"),
            (
                _compat_raise_last_number(installed[package]),
                f"the installed {package} {installed[package]} with its last number raised by one",
            ),
            (
                f"{installed[package]}.1",
                f"the installed {package} {installed[package]} with .1 appended",
            ),
        ]
        shorter = _compat_drop_last_number(installed[package])
        if shorter is None:
            problems.append(
                f"the installed {package} {installed[package]} has a single number, so the probe "
                "with its last number removed cannot be built"
            )
        else:
            probes.append(
                (
                    shorter,
                    f"the installed {package} {installed[package]} with its last number removed",
                )
            )
        for wrong, described in probes:
            expected = {**installed, package: wrong}
            code, error = _compat_run_check(script, expected["aiosqlite"], expected["sqlalchemy"])
            if code == 0:
                problems.append(
                    f"the script exited 0 with {variable}={wrong!r}, {described}, "
                    f"so a wrong {package} version passes"
                )
            elif "Traceback" in error:
                problems.append(
                    f"the script ended in a Python Traceback with {variable}={wrong!r}, "
                    f"{described}, instead of stopping with a message, so it crashes where it "
                    f"should compare: {error.splitlines()[-1]}"
                )
    return problems


def _compat_first_difference(found: str, pinned: str) -> str:
    """Where *found* first departs from *pinned*, with a little of each text around it."""
    at = next(
        (n for n, (a, b) in enumerate(zip(found, pinned)) if a != b), min(len(found), len(pinned))
    )
    return f"they first differ at character {at}: found {found[at : at + 40]!r}, pinned {pinned[at : at + 40]!r}"


def _compat_step_problems(steps: list[dict]) -> list[str]:
    """Why the steps after the replacement are not the pinned assertion step and pytest step."""
    replacing = [i for i, step in enumerate(steps) if step.get("name") == COMPAT_REPLACE_STEP]
    if len(replacing) != 1:
        return [
            f"compat-deps must have exactly one step named {COMPAT_REPLACE_STEP!r}, "
            f"but has {len(replacing)}"
        ]
    after = steps[replacing[0] + 1 :]
    problems = []
    if [step.get("name") for step in after] != [name for name, _, _ in COMPAT_PINNED_STEPS]:
        found = ", ".join(_compat_step_label(step) for step in after) or "none"
        problems.append(
            f"the steps after {COMPAT_REPLACE_STEP!r} must be exactly {COMPAT_ASSERT_STEP!r} "
            f"then {COMPAT_PYTEST_STEP!r}, but they are {found}"
        )
    pinned_runs = {name: (constant, run) for name, constant, run in COMPAT_PINNED_STEPS}
    for step in after:
        label = _compat_step_label(step)
        extra = sorted(set(step) - COMPAT_AFTER_REPLACE_KEYS)
        if extra:
            problems.append(
                f"step {label} has the keys {extra}; after {COMPAT_REPLACE_STEP!r} a step may "
                "have only name, run and timeout-minutes"
            )
        timeout = step.get("timeout-minutes", 1)
        if type(timeout) is not int or timeout < 1:
            problems.append(
                f"step {label} sets timeout-minutes to {timeout!r}; it must be a positive integer"
            )
        if step.get("name") not in pinned_runs:
            continue
        constant, pinned = pinned_runs[step["name"]]
        run = step.get("run")
        if not isinstance(run, str):
            problems.append(f"step {label} has no run text, where {constant} pins one")
        elif run != pinned:
            problems.append(
                f"step {label} has a run text other than {constant}, which this test pins byte "
                f"for byte; {_compat_first_difference(run, pinned)}"
            )
    return problems


def _compat_job_problems(job: dict) -> list[str]:
    """Why the job's own keys, env or strategy are not the pinned ones."""
    problems = []
    extra = sorted(set(job) - COMPAT_JOB_KEYS)
    if extra:
        problems.append(
            f"the compat-deps job has the keys {extra}; it may have only {sorted(COMPAT_JOB_KEYS)}, "
            "so that nothing here changes where its steps run or whether a failure fails the job"
        )
    env = job.get("env")
    if env != COMPAT_JOB_ENV:
        if not isinstance(env, dict):
            problems.append(f"the compat-deps env is {env!r}, not a mapping of three variables")
        else:
            for key in sorted(set(env) | set(COMPAT_JOB_ENV)):
                found = repr(env[key]) if key in env else "unset"
                wanted = repr(COMPAT_JOB_ENV[key]) if key in COMPAT_JOB_ENV else "unset"
                if found != wanted:
                    problems.append(
                        f"the compat-deps env has {key} {found}, but this test pins it to {wanted}"
                    )
    strategy = job.get("strategy")
    others = sorted(set(strategy) - COMPAT_STRATEGY_KEYS) if isinstance(strategy, dict) else []
    if others:
        problems.append(
            f"the compat-deps strategy has the keys {others}; it may have only "
            f"{sorted(COMPAT_STRATEGY_KEYS)}"
        )
    return problems


def _compat_switch_problems(job: dict) -> list[str]:
    """Step settings that let a step be skipped, fail unnoticed or change the later steps' environment."""
    problems = []
    for step in job["steps"]:
        label = _compat_step_label(step)
        if "continue-on-error" in step:
            problems.append(
                f"step {label} sets continue-on-error ({step['continue-on-error']!r}), "
                "so it can fail without failing the job"
            )
        if "if" in step:
            problems.append(
                f"step {label} has an if: condition ({step['if']!r}), so it can be skipped"
            )
        for key in ("shell", "working-directory"):
            if key in step:
                problems.append(
                    f"step {label} sets {key} ({step[key]!r}), which changes how or where its "
                    "run text is run"
                )
        if "GITHUB_ENV" in str(step.get("run", "")):
            problems.append(
                f"step {label} mentions GITHUB_ENV, so it can change the expected versions "
                "or the environment of the steps after it"
            )
        for variable in COMPAT_EXPECTED_VARIABLES:
            if variable in (step.get("env") or {}):
                problems.append(
                    f"step {label} sets {variable} in its own env, so it can expect other "
                    "versions than the matrix row's"
                )
    return problems


def _compat_deps_problems(workflows: Path = WORKFLOWS, pyproject: Path = PYPROJECT) -> list[str]:
    """What the compat-deps job has that the pin refuses.

    Checks the job's keys and env, the strategy and matrix keys, the matrix floors, the
    switches on every step, and that the two steps after the replacement are exactly the
    pinned ones. It does not run the pinned version check; a test of its own does.
    """
    job = _parse((workflows / "ci.yml").read_text())["jobs"]["compat-deps"]
    problems = _compat_job_problems(job)
    problems.extend(_compat_switch_problems(job))
    problems.extend(_compat_step_problems(job["steps"]))
    matrix = job["strategy"]["matrix"]
    others = sorted(set(matrix) - {"include"})
    if others:
        problems.append(
            f"strategy.matrix has the keys {others}; this pin reads only `include`, so other "
            "keys can run versions it does not check"
        )
    rows = matrix.get("include", [])
    for key, extra, name in (
        ("sqlalchemy", "asyncio", "SQLAlchemy"),
        ("aiosqlite", None, "aiosqlite"),
    ):
        declared, floor = _compat_declared_floor(pyproject, key, extra)
        lowest = min(Version(row[key]) for row in rows)
        if floor is None:
            problems.append(f"pyproject.toml declares no >= floor for {declared!r}")
        elif lowest != floor:
            problems.append(
                f"the lowest {name} in the compat-deps matrix is {lowest}, but pyproject.toml "
                f"declares {declared!r}, whose floor is {floor}"
            )
    return problems


def test_the_compat_deps_job_tests_the_versions_it_installs() -> None:
    problems = _compat_deps_problems()
    assert not problems, "\n".join(problems)


def _compat_step(workflow: dict, name: str) -> dict:
    steps = workflow["jobs"]["compat-deps"]["steps"]
    return next(step for step in steps if step.get("name", "").startswith(name))


def _drop_no_sync(name: str):
    def mutate(workflow: dict) -> None:
        step = _compat_step(workflow, name)
        edited = step["run"].replace("uv run --no-sync", "uv run")
        assert edited != step["run"]
        step["run"] = edited

    return mutate


def _delete_step(name: str):
    def mutate(workflow: dict) -> None:
        workflow["jobs"]["compat-deps"]["steps"].remove(_compat_step(workflow, name))

    return mutate


def _raise_the_lowest(key: str):
    def mutate(workflow: dict) -> None:
        rows = workflow["jobs"]["compat-deps"]["strategy"]["matrix"]["include"]
        lowest = min(Version(row[key]) for row in rows)
        for row in rows:
            if Version(row[key]) == lowest:
                row[key] = f"{lowest.major}.{lowest.minor}.{lowest.micro + 1}"

    return mutate


def _set_in_job(*path: str, value):
    def mutate(workflow: dict) -> None:
        node = workflow["jobs"]["compat-deps"]
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _set_in_step(name: str, *path: str, value):
    def mutate(workflow: dict) -> None:
        node = _compat_step(workflow, name)
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _add_step_after(name: str, step: dict):
    def mutate(workflow: dict) -> None:
        steps = workflow["jobs"]["compat-deps"]["steps"]
        steps.insert(steps.index(_compat_step(workflow, name)) + 1, step)

    return mutate


def _swap_steps(first: str, second: str):
    def mutate(workflow: dict) -> None:
        steps = workflow["jobs"]["compat-deps"]["steps"]
        i = steps.index(_compat_step(workflow, first))
        j = steps.index(_compat_step(workflow, second))
        steps[i], steps[j] = steps[j], steps[i]

    return mutate


def _append_to_run(name: str, text: str):
    def mutate(workflow: dict) -> None:
        step = _compat_step(workflow, name)
        step["run"] = step["run"] + text

    return mutate


def _replace_in_run(name: str, old: str, new: str):
    def mutate(workflow: dict) -> None:
        step = _compat_step(workflow, name)
        assert old in step["run"], old
        step["run"] = step["run"].replace(old, new, 1)

    return mutate


# Replacement scripts for the version-check step. Each reads the installed versions.
# The first three do not stop the job when one of them differs from the expected one,
# and the fourth stops it even when they match.
COMPAT_PRINT_ONLY = """\
from importlib.metadata import version
print(version('aiosqlite'), version('sqlalchemy'))
"""
COMPAT_CHECKS_SQLALCHEMY_ONLY = """\
import os
from importlib.metadata import version
if version('sqlalchemy') != os.environ['EXPECTED_SQLALCHEMY']:
    raise SystemExit('not the matrix versions')
"""
COMPAT_CHECKS_AIOSQLITE_ONLY = COMPAT_CHECKS_SQLALCHEMY_ONLY.replace(
    "sqlalchemy", "aiosqlite"
).replace("SQLALCHEMY", "AIOSQLITE")
COMPAT_ONE_LINER = (
    'uv run --no-sync python -c "from importlib.metadata import version; '
    "print(version('aiosqlite'), version('sqlalchemy'))\"\n"
)
COMPAT_ALWAYS_FAILS = """\
from importlib.metadata import version
raise SystemExit('never matches ' + version('aiosqlite'))
"""
# These three exit 0 when the installed versions are the expected ones. The first two
# compare version strings with an ordering operator, so each stops the job on one side
# of the expected version only; the third stops it by raising a NameError, which is a
# crash rather than a comparison.
COMPAT_CHECKS_WITH_LESS_THAN = """\
import os
from importlib.metadata import version
if (version('aiosqlite') < os.environ['EXPECTED_AIOSQLITE']
        or version('sqlalchemy') < os.environ['EXPECTED_SQLALCHEMY']):
    raise SystemExit('older than the matrix')
"""
COMPAT_CHECKS_WITH_GREATER_THAN = COMPAT_CHECKS_WITH_LESS_THAN.replace(" < ", " > ").replace(
    "older", "newer"
)
COMPAT_CRASHES_ON_A_MISMATCH = """\
import os
from importlib.metadata import version
if (version('aiosqlite') != os.environ['EXPECTED_AIOSQLITE']
        or version('sqlalchemy') != os.environ['EXPECTED_SQLALCHEMY']):
    not_the_matrix_versions
"""
# These three also exit 0 on the installed versions. The first compares only the major
# and minor numbers, so a different patch release passes. The second tests whether the
# expected version starts with the installed one, so the installed version followed by
# more text passes. The third tests the other way round, whether the installed version
# starts with the expected one, so a shorter expected version (0.22 for 0.22.1) passes.
COMPAT_CHECKS_MAJOR_MINOR = """\
import os
from importlib.metadata import version
for name in ('aiosqlite', 'sqlalchemy'):
    found = version(name)
    wanted = os.environ['EXPECTED_' + name.upper()]
    if found.split('.')[:2] != wanted.split('.')[:2]:
        raise SystemExit(f'{name} {found} is not the matrix version {wanted}')
"""
COMPAT_CHECKS_STARTSWITH = """\
import os
from importlib.metadata import version
for name in ('aiosqlite', 'sqlalchemy'):
    found = version(name)
    wanted = os.environ['EXPECTED_' + name.upper()]
    if not wanted.startswith(found):
        raise SystemExit(f'{name} {found} is not the matrix version {wanted}')
"""
COMPAT_CHECKS_INSTALLED_STARTSWITH = """\
import os
from importlib.metadata import version
for name in ('aiosqlite', 'sqlalchemy'):
    found = version(name)
    wanted = os.environ['EXPECTED_' + name.upper()]
    if not found.startswith(wanted):
        raise SystemExit(f'{name} {found} is not the matrix version {wanted}')
"""
# A script other than the one in ci.yml that compares both versions exactly and stops
# with a message. It would do what the step is for, and the exact pin refuses it.
COMPAT_ALTERNATIVE_CHECK = """\
import os
import sys
from importlib import metadata

wanted = {
    'aiosqlite': os.environ['EXPECTED_AIOSQLITE'],
    'sqlalchemy': os.environ['EXPECTED_SQLALCHEMY'],
}
wrong = [name for name, value in wanted.items() if metadata.version(name) != value]
if wrong:
    sys.exit('not the matrix versions: ' + ', '.join(wrong))
"""

# (id, edit applied to a copy of the workflow, the parts one reported problem must hold)
COMPAT_ARMS = [
    (
        "pytest-step-syncs",
        _drop_no_sync(COMPAT_PYTEST_STEP),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "assert-step-syncs",
        _drop_no_sync(COMPAT_ASSERT_STEP),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "no-version-assert",
        _delete_step(COMPAT_ASSERT_STEP),
        ["must be exactly", f"but they are {COMPAT_PYTEST_STEP!r}"],
    ),
    ("sqlalchemy-floor-unreached", _raise_the_lowest("sqlalchemy"), ["lowest SQLAlchemy"]),
    ("aiosqlite-floor-unreached", _raise_the_lowest("aiosqlite"), ["lowest aiosqlite"]),
    (
        "check-only-prints",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_PRINT_ONLY)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "check-skips-aiosqlite",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_CHECKS_SQLALCHEMY_ONLY)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "check-skips-sqlalchemy",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_CHECKS_AIOSQLITE_ONLY)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "check-fails-on-a-match",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_ALWAYS_FAILS)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "check-is-a-one-liner",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=COMPAT_ONE_LINER),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "expected-version-not-from-the-matrix",
        _set_in_job("env", "EXPECTED_SQLALCHEMY", value="2.0.38"),
        ["EXPECTED_SQLALCHEMY", "matrix.sqlalchemy"],
    ),
    (
        "job-continue-on-error",
        _set_in_job("continue-on-error", value=True),
        ["job", "continue-on-error"],
    ),
    (
        "step-continue-on-error",
        _set_in_step(COMPAT_PYTEST_STEP, "continue-on-error", value=True),
        [COMPAT_PYTEST_STEP, "continue-on-error"],
    ),
    (
        "step-if",
        _set_in_step(COMPAT_ASSERT_STEP, "if", value="github.event_name == 'push'"),
        [COMPAT_ASSERT_STEP, "if:"],
    ),
    (
        "step-env-overrides-an-expected-version",
        _set_in_step(COMPAT_ASSERT_STEP, "env", value={"EXPECTED_SQLALCHEMY": "2.0.38"}),
        [COMPAT_ASSERT_STEP, "EXPECTED_SQLALCHEMY"],
    ),
    (
        "uv-sync-after-the-install",
        _add_step_after(COMPAT_ASSERT_STEP, {"name": "Restore", "run": "uv sync --all-extras\n"}),
        ["must be exactly", "'Restore'"],
    ),
    (
        "uv-pip-after-the-install",
        _add_step_after(
            COMPAT_ASSERT_STEP,
            {"name": "Pin", "run": "uv pip install --python .venv/bin/python aiosqlite==0.22.1\n"},
        ),
        ["must be exactly", "'Pin'"],
    ),
    (
        "pip-after-the-install",
        _add_step_after(
            COMPAT_ASSERT_STEP, {"name": "Pin", "run": "pip install sqlalchemy==2.0.49\n"}
        ),
        ["must be exactly", "'Pin'"],
    ),
    (
        "python-m-pip-after-the-install",
        _add_step_after(
            COMPAT_ASSERT_STEP, {"name": "Pin", "run": "python -m pip install sqlalchemy==2.0.49\n"}
        ),
        ["must be exactly", "'Pin'"],
    ),
    (
        "matrix-key-other-than-include",
        _set_in_job("strategy", "matrix", "sqlalchemy", value=["2.0.38", "2.1.6"]),
        ["strategy.matrix", "sqlalchemy"],
    ),
    (
        "a-check-with-less-than",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_CHECKS_WITH_LESS_THAN)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "b-check-with-greater-than",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_CHECKS_WITH_GREATER_THAN)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "c-check-that-raises-a-name-error",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_CRASHES_ON_A_MISMATCH)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "d-a-prefix-before-the-heredoc-line",
        _replace_in_run(
            COMPAT_ASSERT_STEP,
            "uv run --no-sync python - <<'PY'",
            "uv run --no-sync touch prepared && uv run --no-sync python - <<'PY'",
        ),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "e-pytest-through-uv-project",
        _set_in_step(
            COMPAT_PYTEST_STEP,
            "run",
            value="uv --project . run pytest -n 0 tests/state/test_compatibility_*.py",
        ),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "f-pytest-through-uvx",
        _set_in_step(
            COMPAT_PYTEST_STEP,
            "run",
            value="uvx --with-editable . pytest -n 0 tests/state/test_compatibility_*.py",
        ),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "g-pytest-step-with-a-second-line",
        _append_to_run(COMPAT_PYTEST_STEP, "\nuv --color auto sync\n"),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "h-a-step-after-pytest",
        _add_step_after(COMPAT_PYTEST_STEP, {"name": "Pin", "run": "pip3.12 install x\n"}),
        ["must be exactly", "'Pin'"],
    ),
    (
        "i-a-command-after-the-heredoc-end",
        _append_to_run(COMPAT_ASSERT_STEP, "python -mpip install x\n"),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "j-a-PY-line-inside-the-script",
        _replace_in_run(COMPAT_ASSERT_STEP, "if mismatched:\n", "PY\nif mismatched:\n"),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "k-github-env-before-the-replacement",
        _add_step_after(
            COMPAT_LOCK_STEP,
            {
                "name": "Expect another",
                "run": 'echo "EXPECTED_SQLALCHEMY=2.0.49" >> "$GITHUB_ENV"\n',
            },
        ),
        ["'Expect another'", "GITHUB_ENV"],
    ),
    (
        "l-shell-on-the-pytest-step",
        _set_in_step(COMPAT_PYTEST_STEP, "shell", value="bash"),
        [COMPAT_PYTEST_STEP, "shell"],
    ),
    (
        "m-working-directory-on-the-assert-step",
        _set_in_step(COMPAT_ASSERT_STEP, "working-directory", value="x"),
        [COMPAT_ASSERT_STEP, "working-directory"],
    ),
    (
        "n-assert-and-pytest-swapped",
        _swap_steps(COMPAT_ASSERT_STEP, COMPAT_PYTEST_STEP),
        ["must be exactly", f"but they are {COMPAT_PYTEST_STEP!r}, {COMPAT_ASSERT_STEP!r}"],
    ),
    (
        "o-defaults-on-the-job",
        _set_in_job("defaults", value={"run": {"shell": "bash", "working-directory": "x"}}),
        ["defaults"],
    ),
    # A check that compares only the major and minor numbers lets a patch release
    # through, and 0.0.0 and 999999.0 differ from every installed version in the first
    # two numbers, so only a difference in the last number shows it.
    (
        "p-check-of-major-and-minor-only",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_CHECKS_MAJOR_MINOR)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    # The pytest options below leave the first four words alone and run no test, or
    # run something other than the tests.
    (
        "q-pytest-that-only-collects-with-co",
        _replace_in_run(COMPAT_PYTEST_STEP, "-n 0", "--co -q -n 0"),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "r-pytest-that-only-collects",
        _replace_in_run(COMPAT_PYTEST_STEP, "-n 0", "--collect-only -n 0"),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "s-pytest-that-prints-help",
        _replace_in_run(COMPAT_PYTEST_STEP, "-n 0", "--help -n 0"),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "t-pytest-that-prints-its-version",
        _replace_in_run(COMPAT_PYTEST_STEP, "-n 0", "--version -n 0"),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    (
        "u-pytest-that-loads-a-plugin",
        _replace_in_run(COMPAT_PYTEST_STEP, "-n 0", "-p some_plugin -n 0"),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    # Both edits leave the job honest, and the exact pin refuses them all the same:
    # changing either step's text means changing its constant in this file.
    (
        "v-another-check-that-compares-both-versions",
        _set_in_step(COMPAT_ASSERT_STEP, "run", value=_compat_run(COMPAT_ALTERNATIVE_CHECK)),
        [COMPAT_ASSERT_STEP, "COMPAT_ASSERT_RUN"],
    ),
    (
        "w-another-pytest-option",
        _replace_in_run(COMPAT_PYTEST_STEP, "--disable-warnings", "--disable-warnings -q"),
        [COMPAT_PYTEST_STEP, "COMPAT_PYTEST_RUN"],
    ),
    # The job's env is exactly three variables.
    (
        "x-uv-python-fixed-to-one-version",
        _set_in_job("env", "UV_PYTHON", value="3.14"),
        ["UV_PYTHON", "matrix.python-version"],
    ),
    (
        "y-an-extra-env-variable-on-the-job",
        _set_in_job("env", "UV_NO_SYNC", value="0"),
        ["UV_NO_SYNC", "unset"],
    ),
    (
        "z-a-missing-env-variable-on-the-job",
        lambda workflow: workflow["jobs"]["compat-deps"]["env"].pop("UV_PYTHON"),
        ["UV_PYTHON", "unset"],
    ),
    # The job's keys are a fixed set.
    (
        "aa-container-on-the-job",
        _set_in_job("container", value="python:3.12"),
        ["container", "may have only"],
    ),
    (
        "ab-services-on-the-job",
        _set_in_job("services", value={"db": {"image": "postgres:16"}}),
        ["services", "may have only"],
    ),
    (
        "ac-a-strategy-key-other-than-fail-fast-and-matrix",
        _set_in_job("strategy", "max-parallel", value=1),
        ["strategy", "max-parallel"],
    ),
    # timeout-minutes on a step after the replacement is a positive integer or absent.
    (
        "ad-a-timeout-of-zero",
        _set_in_step(COMPAT_PYTEST_STEP, "timeout-minutes", value=0),
        [COMPAT_PYTEST_STEP, "timeout-minutes", "positive integer"],
    ),
    (
        "ae-a-timeout-that-is-text",
        _set_in_step(COMPAT_ASSERT_STEP, "timeout-minutes", value="5"),
        [COMPAT_ASSERT_STEP, "timeout-minutes", "positive integer"],
    ),
    (
        "af-a-timeout-that-is-true",
        _set_in_step(COMPAT_ASSERT_STEP, "timeout-minutes", value=True),
        [COMPAT_ASSERT_STEP, "timeout-minutes", "positive integer"],
    ),
]


def _compat_problems_of_a_copy(workflow: dict, tmp_path: Path) -> list[str]:
    """What the pin says about *workflow* written out as a scratch copy of ci.yml."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(yaml.safe_dump(workflow))
    return _compat_deps_problems(workflows)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [pytest.param(mutate, expected, id=arm) for arm, mutate, expected in COMPAT_ARMS],
)
def test_the_compat_deps_pin_refuses_an_edit(mutate, expected: list[str], tmp_path: Path) -> None:
    # Each edit leaves the job parsing and its tests green on whatever versions are
    # installed; only the pin notices that they are no longer the matrix row's, that
    # the check no longer stops the job, or that the matrix no longer reaches the
    # declared floor. The unedited copy passes in `test_the_compat_deps_pin_accepts_
    # these_edits`, which is the control that these messages come from the edit.
    workflow = _load("ci.yml")
    mutate(workflow)
    problems = _compat_problems_of_a_copy(workflow, tmp_path)
    assert any(all(part in problem for part in expected) for problem in problems), problems


def _add_matrix_row(row: dict):
    def mutate(workflow: dict) -> None:
        workflow["jobs"]["compat-deps"]["strategy"]["matrix"]["include"].append(row)

    return mutate


# (id, edit applied to a copy of the workflow); the pin has nothing to say about any of them.
COMPAT_ALLOWED_EDITS = [
    ("an-unedited-copy", lambda workflow: None),
    ("a-timeout-on-the-assert-step", _set_in_step(COMPAT_ASSERT_STEP, "timeout-minutes", value=5)),
    (
        "a-longer-timeout-on-the-pytest-step",
        _set_in_step(COMPAT_PYTEST_STEP, "timeout-minutes", value=30),
    ),
    (
        "steps-before-the-replacement-sync-freely",
        _add_step_after(
            COMPAT_LOCK_STEP,
            {
                "name": "Add a test plugin",
                "run": "uv sync --all-extras\nuv pip install --python .venv/bin/python pytest-timeout\n",
            },
        ),
    ),
    (
        "another-matrix-row-inside-the-range",
        _add_matrix_row({"python-version": "3.12", "aiosqlite": "0.22.1", "sqlalchemy": "2.0.40"}),
    ),
]


@pytest.mark.parametrize(
    "mutate", [pytest.param(mutate, id=edit) for edit, mutate in COMPAT_ALLOWED_EDITS]
)
def test_the_compat_deps_pin_accepts_these_edits(mutate, tmp_path: Path) -> None:
    workflow = _load("ci.yml")
    mutate(workflow)
    assert _compat_problems_of_a_copy(workflow, tmp_path) == []


def test_the_compat_pin_compares_the_text_the_yaml_parser_returns(tmp_path: Path) -> None:
    # The pin compares each step's `run` as parsed, not as spelled. The workflow here
    # holds the version check as one double-quoted scalar with `\n` escapes and the pytest
    # command as another, in place of the block scalars of ci.yml.
    text = (WORKFLOWS / "ci.yml").read_text()
    check_block = re.compile(
        r"      run: \|\n        uv run --no-sync python - <<'PY'\n(?:        .*\n|\n)*?        PY\n"
    )
    pytest_block = re.compile(
        r"      run: >-\n        uv run --no-sync pytest\b(?:.*\n)*?"
        r"        tests/state/test_compatibility_\*\.py\n"
    )
    text, checks = check_block.subn(lambda _: f"      run: {json.dumps(COMPAT_ASSERT_RUN)}\n", text)
    text, pytests = pytest_block.subn(
        lambda _: f"      run: {json.dumps(COMPAT_PYTEST_RUN)}\n", text
    )
    assert (checks, pytests) == (1, 1)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text)
    assert _compat_deps_problems(workflows) == []


def test_the_compat_assertion_script_stops_on_a_wrong_version() -> None:
    # The step's text is pinned to COMPAT_ASSERT_RUN, which is the script below inside a
    # heredoc. A `PY` line in the script would end that heredoc early in the shell, so the
    # script run here would not be the script the job runs.
    assert "PY" not in COMPAT_ASSERT_SCRIPT.splitlines()
    problems = _compat_probe_problems(COMPAT_ASSERT_SCRIPT)
    assert not problems, "\n".join(problems)


# (id, a script, the parts one reported problem must hold). None of these stops the job
# on every wrong version, and each is reported for the first version it lets through.
COMPAT_PROBE_CASES = [
    ("only-prints", COMPAT_PRINT_ONLY, ["exited 0", "EXPECTED_AIOSQLITE", "'0.0.0'"]),
    (
        "checks-sqlalchemy-only",
        COMPAT_CHECKS_SQLALCHEMY_ONLY,
        ["exited 0", "EXPECTED_AIOSQLITE", "'0.0.0'"],
    ),
    (
        "checks-aiosqlite-only",
        COMPAT_CHECKS_AIOSQLITE_ONLY,
        ["exited 0", "EXPECTED_SQLALCHEMY", "'0.0.0'"],
    ),
    ("fails-on-a-match", COMPAT_ALWAYS_FAILS, ["exited 1", "were the installed ones"]),
    ("less-than", COMPAT_CHECKS_WITH_LESS_THAN, ["exited 0", "'0.0.0'", "below every release"]),
    (
        "greater-than",
        COMPAT_CHECKS_WITH_GREATER_THAN,
        ["exited 0", "'999999.0'", "above every release"],
    ),
    ("crashes-on-a-mismatch", COMPAT_CRASHES_ON_A_MISMATCH, ["Traceback", "EXPECTED_AIOSQLITE"]),
    (
        "major-and-minor-only",
        COMPAT_CHECKS_MAJOR_MINOR,
        ["exited 0", "last number raised by one"],
    ),
    # The expected version starts with the installed one: only the `.1` probe gets through.
    ("startswith", COMPAT_CHECKS_STARTSWITH, ["exited 0", ".1 appended"]),
    # The installed version starts with the expected one: 0.0.0, 999999.0, the raised and
    # the `.1` versions are all refused by this check, so only the shortened version
    # passes it.
    (
        "installed-startswith-expected",
        COMPAT_CHECKS_INSTALLED_STARTSWITH,
        ["exited 0", "last number removed"],
    ),
]


@pytest.mark.parametrize(
    ("script", "expected"),
    [pytest.param(script, expected, id=case) for case, script, expected in COMPAT_PROBE_CASES],
)
def test_the_compat_probe_refuses_a_script_that_lets_a_wrong_version_pass(
    script: str, expected: list[str]
) -> None:
    problems = _compat_probe_problems(script)
    assert any(all(part in problem for part in expected) for problem in problems), problems


def test_the_compat_probe_raises_the_last_number_of_a_version() -> None:
    assert _compat_raise_last_number("0.22.1") == "0.22.2"
    assert _compat_raise_last_number("2.0.49") == "2.0.50"
    assert _compat_raise_last_number("2.1.9") == "2.1.10"
    assert _compat_raise_last_number("2.1.0rc1") == "2.1.0rc2"


def test_the_compat_probe_drops_the_last_number_of_a_version() -> None:
    assert _compat_drop_last_number("0.22.1") == "0.22"
    assert _compat_drop_last_number("2.0.52") == "2.0"
    assert _compat_drop_last_number("2.1.0rc1") == "2.1"
    assert _compat_drop_last_number("1.0") == "1"
    assert _compat_drop_last_number("3") is None


def test_the_compat_probe_reports_a_version_it_cannot_shorten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # aiosqlite is reported as installed at the single-number version 3. The script exits 0
    # only for exactly these two expected versions, so every probe that can be built gets
    # the non-zero exit it should, and the one that cannot be built is the only problem.
    installed = {"aiosqlite": "3", "sqlalchemy": "2.0.52"}
    monkeypatch.setattr(importlib.metadata, "version", installed.__getitem__)
    script = (
        "import os, sys\n"
        "sys.exit(0 if (os.environ['EXPECTED_AIOSQLITE'], os.environ['EXPECTED_SQLALCHEMY'])"
        " == ('3', '2.0.52') else 1)\n"
    )
    problems = _compat_probe_problems(script)
    assert len(problems) == 1, problems
    assert "aiosqlite 3 has a single number" in problems[0]
    assert "cannot be built" in problems[0]


@pytest.mark.parametrize(
    ("package", "name"),
    [(r"sqlalchemy\[asyncio\]", "SQLAlchemy"), ("aiosqlite", "aiosqlite")],
)
def test_the_compat_deps_pin_refuses_a_floor_the_matrix_does_not_reach(
    package: str, name: str, tmp_path: Path
) -> None:
    text = PYPROJECT.read_text()
    edited = re.sub(
        rf'("{package}>=\d+\.\d+\.)(\d+)', lambda m: f"{m[1]}{int(m[2]) + 1}", text, count=1
    )
    assert edited != text
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(edited)
    problems = _compat_deps_problems(pyproject=pyproject)
    assert any(f"lowest {name} in" in problem for problem in problems), problems


# The `lionagi/` entries of the `compat` filter are recomputed from the files the
# job runs, so a new import in one of them cannot leave the job skipped for a change
# to the file it imports. The files read are every .py file under lionagi/state and
# the `tests/` files the filter names. An import statement counts wherever it sits
# (inside a function, under TYPE_CHECKING, in a try block). `import lionagi.X` and
# `from lionagi.X import ...` name X. `import lionagi` and `from lionagi import N`
# name lionagi/__init__.py, and also N when lionagi/N is a package or lionagi/N.py a
# module. A relative import counts when it resolves outside lionagi/state. A package
# is listed as `lionagi/X/**` and a module as `lionagi/X.py`.
#
# Importing any lionagi module runs lionagi/__init__.py first, so what that file
# imports when it loads counts as imported by every source above. For that one file
# only the imports that run at module load are read: those under `if TYPE_CHECKING:`
# and those inside a function (including a method) are not. The imports at module load
# are named by the same rule as the rest.
#
# What any of those files import in turn is not followed, and a module named only in a
# string (a `patch` target, an `import_module` argument) is not read.
def _compat_lionagi_entry(names: dict[str, Path], name: str) -> str | None:
    """The filter entry for the package or module of lionagi called *name*, if there is one.

    *names* maps each entry of the lionagi directory to its path. Names are compared as
    spelled, so a case-insensitive file system does not read `Session` as `session`.
    """
    if name in names and names[name].is_dir():
        return f"lionagi/{name}/**"
    if f"{name}.py" in names and names[f"{name}.py"].is_file():
        return f"lionagi/{name}.py"
    return None


def _compat_is_type_checking(test: ast.expr) -> bool:
    """Whether *test* is `TYPE_CHECKING` or `<module>.TYPE_CHECKING`."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _compat_module_load_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """The import statements of *tree* that run when its module loads.

    The body of an `if TYPE_CHECKING:` and anything inside a function are left out; its
    `else` branch, a class body and a `try` block are read.
    """
    found: list[ast.Import | ast.ImportFrom] = []
    pending = list(ast.iter_child_nodes(tree))
    while pending:
        node = pending.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        elif isinstance(node, ast.If) and _compat_is_type_checking(node.test):
            pending.extend(node.orelse)
        else:
            pending.extend(ast.iter_child_nodes(node))
    return found


def _compat_names_in(
    source: Path, package: list[str] | None, names: dict[str, Path], at_load: bool = False
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """The filter entries the imports in *source* name, and the lionagi names that resolve to none.

    *package* is the dotted package the file sits in, as a list, or None for a file whose
    relative imports do not name lionagi. Both results carry the line of the import. Every
    import statement counts, unless *at_load* is set: then only those that run when the
    module loads do.
    """
    found: list[tuple[str, int]] = []
    unresolved: list[tuple[str, int]] = []

    def name(parts: list[str], imported: list[str], line: int) -> None:
        if not parts or parts[0] != "lionagi":
            return
        if len(parts) == 1:
            found.append(("lionagi/__init__.py", line))
            for item in imported:
                entry = _compat_lionagi_entry(names, item)
                if entry:
                    found.append((entry, line))
            return
        entry = _compat_lionagi_entry(names, parts[1])
        if entry:
            found.append((entry, line))
        else:
            unresolved.append((f"lionagi.{parts[1]}", line))

    tree = ast.parse(source.read_text(), filename=str(source))
    for node in _compat_module_load_imports(tree) if at_load else ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name(alias.name.split("."), [], node.lineno)
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            if node.level:
                if package is None:
                    continue
                parts = package[: max(len(package) - (node.level - 1), 0)] + parts
            name(parts, [alias.name for alias in node.names], node.lineno)
    return found, unresolved


def _compat_filter_problems(
    workflows: Path = WORKFLOWS,
    root: Path = REPO_ROOT,
    state: Path | None = None,
    init: Path | None = None,
) -> list[str]:
    """How the compat filter's `lionagi/` entries differ from what its sources import.

    *state* is the directory read as lionagi/state, *init* the file read as
    lionagi/__init__.py, and the `tests/` files come from the filter's own entries,
    globbed under *root*.
    """
    state = state or root / "lionagi" / "state"
    init = init or root / "lionagi" / "__init__.py"
    listed = _changes_filters(workflows)["compat"]
    names = {path.name: path for path in (root / "lionagi").iterdir()}
    sources: list[tuple[str, Path, list[str] | None]] = []
    for path in sorted(state.rglob("*.py")):
        relative = path.relative_to(state)
        sources.append(
            (
                f"lionagi/state/{relative.as_posix()}",
                path,
                ["lionagi", "state", *relative.parent.parts],
            )
        )
    problems = []
    for pattern in listed:
        if not pattern.startswith("tests/"):
            continue
        matched = sorted(root.glob(pattern))
        if not matched:
            problems.append(f"the compat filter names {pattern!r}, which matches no file")
        sources.extend((p.relative_to(root).as_posix(), p, None) for p in matched)
    needed = {"lionagi/state/**": "the lionagi/state tree itself"}
    for label, path, package in sources:
        found, unresolved = _compat_names_in(path, package, names)
        for entry, line in found:
            needed.setdefault(entry, f"{label}:{line}")
        for name, line in unresolved:
            problems.append(
                f"{label}:{line} imports {name}, which is neither a package nor a module of lionagi"
            )
    found, unresolved = _compat_names_in(init, ["lionagi"], names, at_load=True)
    for entry, line in found:
        needed.setdefault(entry, f"the module-load import at lionagi/__init__.py:{line}")
    for name, line in unresolved:
        problems.append(
            f"lionagi/__init__.py:{line} imports {name}, which is neither a package nor a "
            "module of lionagi"
        )
    present = {entry for entry in listed if entry.startswith("lionagi/")}
    for entry in sorted(set(needed) - present):
        problems.append(f"the compat filter lacks {entry!r}, which {needed[entry]} needs")
    for entry in sorted(present - set(needed)):
        problems.append(
            f"the compat filter lists {entry!r}, which none of the {len(sources)} files read "
            "for it import, and lionagi/__init__.py does not import at module load"
        )
    return problems


def test_the_compat_filter_lists_what_its_sources_import() -> None:
    problems = _compat_filter_problems()
    assert not problems, "\n".join(problems)


def _copy_the_state_tree(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    shutil.copytree(
        REPO_ROOT / "lionagi" / "state", state, ignore=shutil.ignore_patterns("__pycache__")
    )
    return state


def test_the_compat_filter_pin_refuses_an_import_the_filter_lacks(tmp_path: Path) -> None:
    # The same state tree, copied, passes; one more import in a copy of it, of a real
    # package the filter does not list, is reported by name and by the file that has it.
    package = "adapters"
    assert (REPO_ROOT / "lionagi" / package / "__init__.py").is_file()
    state = _copy_the_state_tree(tmp_path)
    assert _compat_filter_problems(state=state) == []
    with (state / "compat.py").open("a") as handle:
        handle.write(f"\nimport lionagi.{package}\n")
    problems = _compat_filter_problems(state=state)
    assert any(
        f"lacks 'lionagi/{package}/**'" in problem and "lionagi/state/compat.py:" in problem
        for problem in problems
    ), problems


def test_the_compat_filter_pin_refuses_an_entry_nothing_imports(tmp_path: Path) -> None:
    package = "tools"
    old = "            - 'lionagi/testing/**'\n"
    text = _ci_gate_text()
    assert text.count(old) == 1
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        text.replace(old, old + f"            - 'lionagi/{package}/**'\n")
    )
    problems = _compat_filter_problems(workflows)
    assert any(f"lists 'lionagi/{package}/**'" in problem for problem in problems), problems


def test_the_compat_filter_pin_refuses_a_dropped_entry(tmp_path: Path) -> None:
    old = "            - 'lionagi/plugins/**'\n"
    text = _ci_gate_text()
    assert text.count(old) == 1
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text.replace(old, ""))
    problems = _compat_filter_problems(workflows)
    assert any("lacks 'lionagi/plugins/**'" in problem for problem in problems), problems


@pytest.mark.parametrize("entry", ["lionagi/_moved.py", "lionagi/version.py"])
def test_the_compat_filter_pin_refuses_a_dropped_module_load_entry(
    entry: str, tmp_path: Path
) -> None:
    # lionagi/__init__.py imports these when it loads, which happens before any other
    # lionagi module runs, so the filter lists them although no source names them.
    old = f"            - '{entry}'\n"
    text = _ci_gate_text()
    assert text.count(old) == 1
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text.replace(old, ""))
    problems = _compat_filter_problems(workflows)
    assert [problem for problem in problems if f"lacks {entry!r}" in problem] != [], problems
    assert all("lionagi/__init__.py:" in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("placement", "refused"),
    [
        pytest.param("\nfrom . import utils as _utils\n", True, id="module-level"),
        pytest.param(
            "\ntry:\n    from . import utils as _utils\nexcept ImportError:\n    pass\n",
            True,
            id="in-a-try-block",
        ),
        pytest.param(
            "\nif TYPE_CHECKING:\n    from . import utils as _utils\n",
            False,
            id="under-type-checking",
        ),
        pytest.param(
            "\ndef _late():\n    from . import utils as _utils\n", False, id="inside-a-function"
        ),
    ],
)
def test_the_compat_filter_pin_reads_the_module_load_imports_of_the_package_init(
    placement: str, refused: bool, tmp_path: Path
) -> None:
    # A copy of lionagi/__init__.py passes as it is. One more import of a real module the
    # filter does not list is refused by that module's entry when it runs at module load,
    # and is left alone under `if TYPE_CHECKING:` or inside a function.
    module = "utils"
    assert (REPO_ROOT / "lionagi" / f"{module}.py").is_file()
    assert f"lionagi/{module}.py" not in _changes_filters(WORKFLOWS)["compat"]
    init = tmp_path / "__init__.py"
    shutil.copyfile(REPO_ROOT / "lionagi" / "__init__.py", init)
    assert _compat_filter_problems(init=init) == []
    with init.open("a") as handle:
        handle.write(placement)
    problems = _compat_filter_problems(init=init)
    if refused:
        assert len(problems) == 1, problems
        assert f"lacks 'lionagi/{module}.py'" in problems[0], problems
        assert "lionagi/__init__.py:" in problems[0], problems
    else:
        assert problems == []


@pytest.mark.parametrize(
    ("code", "package", "expected"),
    [
        pytest.param(
            "import lionagi.ln.concurrency\n",
            ["lionagi", "state"],
            {"lionagi/ln/**"},
            id="absolute-import",
        ),
        pytest.param(
            "from lionagi._paths import HOME\n",
            ["lionagi", "state"],
            {"lionagi/_paths.py"},
            id="absolute-from-a-module",
        ),
        pytest.param(
            "import lionagi\n", ["lionagi", "state"], {"lionagi/__init__.py"}, id="bare-import"
        ),
        pytest.param(
            "from lionagi import Branch\n", None, {"lionagi/__init__.py"}, id="from-lionagi-a-class"
        ),
        pytest.param(
            "from lionagi import ln, _paths\n",
            None,
            {"lionagi/__init__.py", "lionagi/ln/**", "lionagi/_paths.py"},
            id="from-lionagi-a-package-and-a-module",
        ),
        pytest.param(
            "from lionagi import Session\n",
            None,
            {"lionagi/__init__.py"},
            id="names-are-compared-as-spelled",
        ),
        pytest.param(
            "def f():\n    from lionagi.hooks import x\n",
            None,
            {"lionagi/hooks/**"},
            id="inside-a-function",
        ),
        pytest.param(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from lionagi.hooks import x\n",
            None,
            {"lionagi/hooks/**"},
            id="under-type-checking",
        ),
        pytest.param(
            "from ...ln import x\n",
            ["lionagi", "state", "sub"],
            {"lionagi/ln/**"},
            id="relative-leaving-state",
        ),
        pytest.param(
            "from ... import _paths\n",
            ["lionagi", "state", "sub"],
            {"lionagi/__init__.py", "lionagi/_paths.py"},
            id="relative-to-lionagi",
        ),
        pytest.param(
            "from . import a\nfrom .. import b\nfrom ..db import c\n",
            ["lionagi", "state", "sub"],
            {"lionagi/state/**"},
            id="relative-inside-state",
        ),
        pytest.param("from . import ln\n", None, set(), id="relative-in-a-test-file"),
        pytest.param(
            "import json\nfrom yaml import safe_load\n",
            ["lionagi", "state"],
            set(),
            id="other-packages",
        ),
    ],
)
def test_the_compat_import_rule(code: str, package, expected: set[str], tmp_path: Path) -> None:
    lionagi = tmp_path / "lionagi"
    for directory in ("ln", "hooks", "session", "state"):
        (lionagi / directory).mkdir(parents=True)
        (lionagi / directory / "__init__.py").write_text("")
    (lionagi / "_paths.py").write_text("")
    source = tmp_path / "source.py"
    source.write_text(code)
    names = {path.name: path for path in lionagi.iterdir()}
    found, unresolved = _compat_names_in(source, package, names)
    assert {entry for entry, _ in found} == expected
    assert unresolved == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param("from ._paths import HOME\n", {"lionagi/_paths.py"}, id="module-level"),
        pytest.param(
            "from . import ln\n", {"lionagi/__init__.py", "lionagi/ln/**"}, id="package-by-from-dot"
        ),
        pytest.param("from .ln.types import Spec\n", {"lionagi/ln/**"}, id="inside-a-package"),
        pytest.param("import logging\nimport yaml\n", set(), id="other-packages"),
        pytest.param(
            "try:\n    from ._paths import HOME\nexcept ImportError:\n    pass\n",
            {"lionagi/_paths.py"},
            id="in-a-try-block",
        ),
        pytest.param(
            "class C:\n    from ._paths import HOME\n", {"lionagi/_paths.py"}, id="in-a-class"
        ),
        pytest.param(
            "if not TYPE_CHECKING:\n    from ._paths import HOME\n",
            {"lionagi/_paths.py"},
            id="when-not-type-checking",
        ),
        pytest.param(
            "if TYPE_CHECKING:\n    pass\nelse:\n    from ._paths import HOME\n",
            {"lionagi/_paths.py"},
            id="else-of-type-checking",
        ),
        pytest.param(
            "if TYPE_CHECKING:\n    from ._paths import HOME\n", set(), id="under-type-checking"
        ),
        pytest.param(
            "import typing\nif typing.TYPE_CHECKING:\n    from ._paths import HOME\n",
            set(),
            id="under-typing-type-checking",
        ),
        pytest.param(
            "if TYPE_CHECKING:\n    if x:\n        from ._paths import HOME\n",
            set(),
            id="nested-under-type-checking",
        ),
        pytest.param("def f():\n    from ._paths import HOME\n", set(), id="inside-a-function"),
        pytest.param(
            "async def f():\n    from ._paths import HOME\n", set(), id="inside-an-async-function"
        ),
        pytest.param(
            "class C:\n    def m(self):\n        from ._paths import HOME\n",
            set(),
            id="inside-a-method",
        ),
    ],
)
def test_the_compat_module_load_rule(code: str, expected: set[str], tmp_path: Path) -> None:
    # For lionagi/__init__.py only the imports that run when the module loads count.
    lionagi = tmp_path / "lionagi"
    for directory in ("ln", "hooks", "session", "state"):
        (lionagi / directory).mkdir(parents=True)
        (lionagi / directory / "__init__.py").write_text("")
    (lionagi / "_paths.py").write_text("")
    source = tmp_path / "source.py"
    source.write_text(code)
    names = {path.name: path for path in lionagi.iterdir()}
    found, unresolved = _compat_names_in(source, ["lionagi"], names, at_load=True)
    assert {entry for entry, _ in found} == expected
    assert unresolved == []


def test_the_compat_import_rule_reports_a_name_that_is_not_in_lionagi(tmp_path: Path) -> None:
    (tmp_path / "lionagi").mkdir()
    source = tmp_path / "source.py"
    source.write_text("import lionagi.nothing_here\n")
    found, unresolved = _compat_names_in(source, None, {})
    assert found == []
    assert unresolved == [("lionagi.nothing_here", 1)]
