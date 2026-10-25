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
import json
import posixpath
import re
import textwrap
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

# A conditional job is allowed to skip only when the path-filter output that gates
# it reports its inputs unchanged; the flag it is read against is pinned with it.
CI_GATE_CONDITIONAL_GATES = {
    "studio-docker": "studio",
    "minimal-import": "imports",
    "no-eager-heavies": "imports",
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
    "lionrs": ["lionrs/**", ".github/workflows/ci.yml"],
    "lion1": ["lion1/**", ".github/workflows/ci.yml"],
}
CI_CHANGES_OUTPUTS = {
    name: f"${{{{ steps.filter.outputs.{name} }}}}" for name in CI_CHANGES_FILTERS
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
    # the required check then passes with the job never run on its own paths.
    assert _changes_filters() == CI_CHANGES_FILTERS


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
    ],
    ids=["lion1", "studio", "imports", "adr", "lionrs"],
)
def test_the_changes_job_pin_refuses_a_dropped_path(path: str, tmp_path: Path) -> None:
    # One arm per filter, so dropping a path from any of them is caught: a path
    # shared by several filters would hide a drop from one alone.
    text = _ci_gate_text().replace(path, "", 1)
    assert text != _ci_gate_text()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text)
    assert _changes_filters(workflows) != CI_CHANGES_FILTERS


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
