"""Design records cite code by symbol, or by a pin that never moves.

A citation of the form ``path.py:120`` goes stale as soon as a line is inserted above
line 120, so every such change would have to renumber the records. The records cite
code in one of four forms instead:

* a symbol, ``path.py::Class.method`` (or ``path.py::NAME`` for a module-level binding),
  which names the code and does not move;
* a quote, a ``path.py`` span followed by a short code span holding one line of that file,
  which must occur exactly once in the file;
* a pin, ``path.py:N@<hash8>``, for code that was rewritten or removed so that no symbol
  holds it any more: the commit fixes the file, so line N never moves;
* a module, ``path.py`` alone, for a record that means the whole file: it carries no line
  and no name, so nothing in it can go stale. The file it names must still be tracked.

Eight checks keep that true. Each is a plain function over the record texts and a
``Source`` (the tracked files, their current text, and the file text at a commit), so a
small in-memory fixture can carry exactly one defect and a test can require the check to
report it. The tests over the real records use the repository checkout as the source.

A line number written after a citation token outside backticks (``path.py::name`, :12``) is
reported unless the token is a repository path to a file that is not Python. A code span
that holds only a line number (``:12`` or ``:15-18``) is reported when the nearest path
before it in the same paragraph is a Python file.

A module citation is a repository path to a Python file that is the whole content of a list
item or of a table cell. A path inside running text may name a file that was retired or is
still planned, so only the list item and the table cell are checked against the tracked
files.

A symbol citation is checked for existing in a tracked file, not for being the code its
sentence describes, and the checks read citations of Python files only.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"

# The one record that quotes edits made against a fixed tree: every citation in it is a
# pin, and the pins are exempt from the list below.
EDIT_RECORD = "edits/ADR-0126-amendment-9-edits.md"

# Pins outside the edit record, by record file (relative to docs/adr). A pin is allowed
# while the cited code has no symbol at the head in that file, or while the sentence says
# where the code stood at the pinned commit. A new pin fails
# test_pins_outside_the_edit_record_are_listed, and a pin that is gone from its record
# fails it too, so the list always matches the records: when a cited symbol returns,
# replace the pin with the symbol and delete its entry here.
KNOWN_PINS: dict[str, tuple[str, ...]] = {
    "ADR-0066-li-mcp-v2-verb-surface.md": (
        "lionagi/cli/kill.py:378@95b559a4",
        "lionagi/cli/kill.py:383@95b559a4",
        "lionagi/cli/kill.py:392-418@95b559a4",
    ),
    "ADR-0069-reactive-flow-steering-and-recovery.md": (
        "lionagi/cli/_runs.py:631-634@ee589cb2",
        "lionagi/cli/orchestrate/_checkpoint.py:100@ee589cb2",
        "lionagi/cli/orchestrate/_checkpoint.py:136@ee589cb2",
        "lionagi/cli/orchestrate/_checkpoint.py:188@ee589cb2",
        "lionagi/cli/orchestrate/_checkpoint.py:80@ee589cb2",
        "lionagi/cli/orchestrate/_manifest.py:333@72461356",
    ),
    "ADR-0117-normalized-progression-membership-and-online-cutover.md": (
        "lionagi/cli/_runs.py:1922@e1800c3b",
        "lionagi/cli/_runs.py:2236-2237@e1800c3b",
    ),
    "ADR-0126-provider-credential-routing-and-monthly-account-budgets.md": (
        "lionagi/providers/anthropic/messages.py:511-541@04e03ffe",
        "lionagi/providers/anthropic/messages.py:981-1011@04e03ffe",
        "lionagi/service/accounts/_ledger.py:548@c6ac2d97",
    ),
    "ADR-0130-per-request-input-ceiling-and-context-management-past-it.md": (
        "lionagi/providers/anthropic/messages.py:988@2a8b69c9",
    ),
    "ADR-0131-operator-decisions-require-a-launcher-issued-decision-capability.md": (
        "lionagi/studio/cli.py:274@124b1419",
        "lionagi/studio/launch_access.py:49@124b1419",
        "lionagi/studio/launch_access.py:63@124b1419",
        "tests/studio/test_hosted_hardening.py:967@124b1419",
    ),
    "ADR-0136-moving-non-core-packages-into-lionhub.md": (
        "lionagi/_auto.py:157-161@5f3672b2",
        "lionagi/_auto.py:175@5f3672b2",
        "lionagi/_auto.py:180@5f3672b2",
        "lionagi/lndl/extract.py:12-27@c4abb363",
        "lionagi/operations/flow.py:1126@c4abb363",
        "lionagi/operations/flow.py:1162@c4abb363",
        "lionagi/operations/flow.py:1228@c4abb363",
        "lionagi/operations/flow.py:1306@c4abb363",
        "lionagi/operations/lndl_middle/lndl_middle.py:16@0e6f1979",
        "lionagi/operations/parse/parse.py:315@c4abb363",
        "lionagi/providers/_secret_resolution.py:101-104@c4abb363",
        "lionagi/providers/_secret_resolution.py:101@c4abb363",
        "lionagi/session/signal.py:280@c4abb363",
        "lionagi/state/lifecycle/notify_settings.py:383@1e7be920",
        "lionagi/state/lifecycle/notify_settings.py:41@1e7be920",
        "lionagi/state/provenance.py:19@1e7be920",
        "lionagi/studio/scheduler/subprocess.py:279@c4abb363",
    ),
    "ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md": (
        "lionagi/cli/_schedule_parsing.py:32-79@fa46d0e8",
        "lionagi/cli/_schedule_parsing.py:358@fa46d0e8",
        "lionagi/cli/_schedule_parsing.py:429-441@fa46d0e8",
        "lionagi/cli/machine_schedule.py:136@fa46d0e8",
        "lionagi/cli/machine_schedule.py:38@fa46d0e8",
        "lionagi/cli/machine_schedule.py:443@fa46d0e8",
        "lionagi/cli/machine_schedule.py:515@fa46d0e8",
        "lionagi/cli/machine_schedule.py:73-76@fa46d0e8",
        "lionagi/cli/mirror.py:21@b4a3bebf",
        "lionagi/studio/app.py:349-364@8c119ecc",
        "lionagi/studio/app.py:349-364@b4a3bebf",
        "lionagi/studio/cli.py:1203-1226@b4a3bebf",
        "lionagi/studio/services/schedule_declaration.py:482@8c119ecc",
        "lionagi/studio/services/schedule_declaration.py:482@b4a3bebf",
        "lionagi/studio/services/schedule_declaration.py:534@b4a3bebf",
        "tests/test_moved_modules.py:669@b4a3bebf",
    ),
    "ADR-0139-cli-package-location-and-console-script-ownership.md": (
        "lionagi/_auto.py:143@2a49c9fe",
        "lionagi/cli/_argtypes.py:23@327665f3",
        "lionagi/cli/_code_identity.py:55@327665f3",
        "lionagi/cli/_engine_definitions.py:19@327665f3",
        "lionagi/cli/_inspection.py:1@327665f3",
        "lionagi/cli/_inspection.py:23@8ed68552",
        "lionagi/cli/_inspection.py:265@8ed68552",
        "lionagi/cli/_inspection.py:38@8ed68552",
        "lionagi/cli/_inspection.py:44@8ed68552",
        "lionagi/cli/_kill_service.py:42@327665f3",
        "lionagi/cli/_logging.py:1@327665f3",
        "lionagi/cli/_mcp_resolve.py:22@327665f3",
        "lionagi/cli/_mirror_service.py:28@feb2f032",
        "lionagi/cli/_mirror_service.py:43@327665f3",
        "lionagi/cli/_mirror_settings.py:16@327665f3",
        "lionagi/cli/_mirror_settings.py:16@feb2f032",
        "lionagi/cli/_orchestrate_parsing.py:10@4af5b0a9",
        "lionagi/cli/_orchestrate_parsing.py:10@2ced5e58",
        "lionagi/cli/_orchestrate_parsing.py:174@4af5b0a9",
        "lionagi/cli/_orchestrate_parsing.py:1@327665f3",
        "lionagi/cli/_orchestrate_parsing.py:398@4af5b0a9",
        "lionagi/cli/_orchestrate_parsing.py:399@4af5b0a9",
        "lionagi/cli/_orchestrate_parsing.py:41@4af5b0a9",
        "lionagi/cli/_orchestrate_parsing.py:42@2ced5e58",
        "lionagi/cli/_orchestrate_parsing.py:7@4af5b0a9",
        "lionagi/cli/_orchestrate_parsing.py:7@e1a5568e",
        "lionagi/cli/_project.py:19@327665f3",
        "lionagi/cli/_providers.py:184@327665f3",
        "lionagi/cli/_runs.py:1499@5efc2c75",
        "lionagi/cli/_runs.py:1922@feb2f032",
        "lionagi/cli/_runs.py:2053@5efc2c75",
        "lionagi/cli/_runs.py:2273@5efc2c75",
        "lionagi/cli/_runs.py:24@5efc2c75",
        "lionagi/cli/_runs.py:37@327665f3",
        "lionagi/cli/_runs.py:37@5efc2c75",
        "lionagi/cli/_schedule_parsing.py:1@327665f3",
        "lionagi/cli/_team_service.py:16@327665f3",
        "lionagi/cli/_util.py:14@327665f3",
        "lionagi/cli/main.py:32-33@2a49c9fe",
        "lionagi/cli/main.py:370@2a49c9fe",
        "lionagi/cli/orchestrate/__init__.py:26@4af5b0a9",
        "lionagi/cli/orchestrate/__init__.py:27-28@2ced5e58",
        "lionagi/cli/orchestrate/__init__.py:27@4af5b0a9",
        "lionagi/cli/orchestrate/__init__.py:425@1717674a",
        "lionagi/cli/orchestrate/_checkpoint.py:21@327665f3",
        "lionagi/cli/orchestrate/_checkpoint.py:242@2ced5e58",
        "lionagi/cli/orchestrate/_orchestration.py:2137@1717674a",
        "lionagi/cli/status.py:17@8ed68552",
        "lionagi/cli/status.py:29@8ed68552",
        "lionagi/studio/scheduler/subprocess.py:251@2ced5e58",
        "lionagi/studio/scheduler/subprocess.py:279@1717674a",
        "lionagi/studio/scheduler/subprocess.py:279@2a49c9fe",
    ),
}

# Symbols a Proposed record asks for that do not exist yet, by record file. A name is allowed
# here only while the head holds no such code; once it exists the entry fails and must go.
PLANNED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "ADR-0141-recorded-chain-decisions-for-schedule-runs.md": (
        "tests/studio/test_scheduler_engine.py::"
        "test_normal_path_terminal_write_records_chain_decision",
        "tests/studio/test_scheduler_engine.py::"
        "test_recovery_terminal_write_records_chain_decision",
        "tests/studio/test_scheduler_engine.py::test_selection_is_none_at_depth_cap",
        "tests/state/lifecycle/test_schedule_adapter.py::"
        "test_schedule_run_status_write_keeps_reason_columns",
        "tests/studio/test_scheduler_engine.py::test_lost_terminal_cas_does_not_chain_normal_path",
        "tests/cli/test_monitor_run.py::test_chain_decision_none_returns_without_sleep",
        "tests/cli/test_monitor_run.py::test_chain_decision_branch_follows_child",
        "tests/cli/test_monitor_run.py::test_chain_decision_survives_schedule_edit",
        "tests/cli/test_monitor_run.py::"
        "test_chain_decision_unknown_version_is_legacy_with_one_warning",
    ),
}

_PATH = r"[\w./-]+\.py"
# ``path.py:N``, ``path.py:N-M``, either with an optional ``@<hash>`` pin.
_LINE = re.compile(
    rf"(?<![\w./-])(?P<path>{_PATH}):(?P<lo>\d+)(?:[-–](?P<hi>\d+))?(?:@(?P<sha>[0-9a-f]+))?"
)
# ``path.py::Class.method`` and ``path.py::NAME``.
_SYMBOL = re.compile(rf"(?<![\w./-])(?P<path>{_PATH})::(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")
# `path.py` followed by one backticked line of that file.
_QUOTE = re.compile(rf"`(?P<path>{_PATH})` `(?P<quote>[^`\n]+)`")
# A code span followed by line numbers written outside backticks: ``path.py::name`, :12; :15-18``.
_BARE_RUN = re.compile(r"`(?P<span>[^`\n]+)`(?P<run>(?:[,;] :\d+(?:[-–]\d+)?(?![\w@]))+)")
_BARE_MEMBER = re.compile(r"[,;] (?P<number>:\d+(?:[-–]\d+)?)")
# The path a code span starts with, when it is a repository path to a file that is not Python.
_OTHER_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+\.(?!pyi?$)[A-Za-z]\w*")
# A Python path that is the whole content of a list item, or of one cell of a table row.
_ITEM = re.compile(rf"\s*(?:[-*+]|\d+[.)])\s+`(?P<path>{_PATH})`\s*")
_CELL = re.compile(rf"\s*`(?P<path>{_PATH})`\s*")
# A code span that holds only a line number: ``:12`` or ``:15-18``.
_FLOATING = re.compile(r"`:(?P<number>\d+(?:[-–]\d+)?)`")
# A path to a file of a kind the records cite, with or without a symbol or line after it.
_ANY_PATH = re.compile(
    r"(?<![\w./-])(?P<path>[\w./-]+\.(?:py|sql|tsx|ts|md|toml|rs|yaml|yml|json|js|sh|cfg))(?!\w)"
)


class Source(Protocol):
    """What the checks need to know about the code the records cite."""

    def tracked(self) -> frozenset[str]: ...

    def head_text(self, path: str) -> str | None: ...

    def commit_exists(self, sha: str) -> bool: ...

    def text_at(self, sha: str, path: str) -> str | None: ...


@dataclass
class MemorySource:
    """An in-memory source: the current files and the files at named commits."""

    files: dict[str, str]
    commits: dict[str, dict[str, str]] = field(default_factory=dict)

    def tracked(self) -> frozenset[str]:
        return frozenset(self.files)

    def head_text(self, path: str) -> str | None:
        return self.files.get(path)

    def commit_exists(self, sha: str) -> bool:
        return sha in self.commits

    def text_at(self, sha: str, path: str) -> str | None:
        return self.commits.get(sha, {}).get(path)


class GitSource:
    """The repository checkout, read through read-only git commands."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._tracked: frozenset[str] | None = None
        self._commits: dict[str, bool] = {}
        self._texts: dict[tuple[str, str], str | None] = {}

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def available(self) -> bool:
        try:
            return self._git("rev-parse", "--git-dir").returncode == 0
        except OSError:
            return False

    def is_shallow(self) -> bool:
        return self._git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"

    def tracked(self) -> frozenset[str]:
        if self._tracked is None:
            listed = self._git("ls-files", "-z").stdout
            self._tracked = frozenset(name for name in listed.split("\0") if name)
        return self._tracked

    def head_text(self, path: str) -> str | None:
        if path not in self.tracked():
            return None
        try:
            return (self.root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def commit_exists(self, sha: str) -> bool:
        if sha not in self._commits:
            done = self._git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
            self._commits[sha] = done.returncode == 0
        return self._commits[sha]

    def text_at(self, sha: str, path: str) -> str | None:
        key = (sha, path)
        if key not in self._texts:
            done = self._git("show", f"{sha}:{path}")
            self._texts[key] = done.stdout if done.returncode == 0 else None
        return self._texts[key]


def load_records(adr_dir: Path) -> dict[str, str]:
    """Every record under ``adr_dir``, keyed by its path relative to it."""
    return {
        path.relative_to(adr_dir).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(adr_dir.rglob("*.md"))
    }


def _lineno(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def pin_tokens(records: dict[str, str]) -> list[tuple[str, int, re.Match[str]]]:
    return [
        (name, _lineno(text, m.start()), m)
        for name, text in records.items()
        for m in _LINE.finditer(text)
        if m.group("sha") is not None
    ]


def symbol_tokens(records: dict[str, str]) -> list[tuple[str, int, re.Match[str]]]:
    return [
        (name, _lineno(text, m.start()), m)
        for name, text in records.items()
        for m in _SYMBOL.finditer(text)
    ]


def quote_tokens(records: dict[str, str], source: Source) -> list[tuple[str, int, re.Match[str]]]:
    """Quoted citations: a full repository path and a code span, side by side.

    A path that does not start at a top-level directory of the repository (a bare
    ``module.py`` or ``pkg/module.py`` written as a table column) names no file by itself,
    so it is prose and not a citation.
    """
    roots = {path.split("/", 1)[0] for path in source.tracked() if "/" in path}
    return [
        (name, _lineno(text, m.start()), m)
        for name, text in records.items()
        for m in _QUOTE.finditer(text)
        if "/" in m.group("path") and m.group("path").split("/", 1)[0] in roots
    ]


def _qualnames(text: str) -> set[str]:
    """Dotted names of every def, async def and class, plus the module-level bindings."""
    tree = ast.parse(text)
    names: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = prefix + child.name
                names.add(qual)
                walk(child, qual + ".")
            elif isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                walk(child, prefix)

    walk(tree, "")
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name.split(".")[0]) for a in node.names)
    return names


def check_no_line_citations(records: dict[str, str]) -> list[str]:
    """Check 1: no ``path.py:N`` remains without a ``@<hash>`` pin."""
    return [
        f"{name}:{line}: {m.group(0)} cites a line number; cite a symbol or pin it"
        for name, text in records.items()
        for m in _LINE.finditer(text)
        if m.group("sha") is None
        for line in [_lineno(text, m.start())]
    ]


def check_symbols_exist(
    records: dict[str, str],
    source: Source,
    planned: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """Check 2: every ``path::qualname`` names a tracked file that holds that qualname.

    ``planned`` lists names a record gives to code that does not exist yet; such a name
    passes while it is absent, and fails once the code exists, so the entry is removed.
    """
    problems: list[str] = []
    cache: dict[str, set[str] | str] = {}
    planned = planned or {}
    cited: dict[str, set[str]] = {}
    for name, line, m in symbol_tokens(records):
        path, qual = m.group("path"), m.group("name")
        listed = m.group(0) in planned.get(name, ())
        cited.setdefault(name, set()).add(m.group(0))
        if path not in cache:
            text = source.head_text(path) if path in source.tracked() else None
            if text is None:
                cache[path] = "is not a tracked file"
            else:
                try:
                    cache[path] = _qualnames(text)
                except SyntaxError:
                    cache[path] = "does not parse"
        held = cache[path]
        resolves = not isinstance(held, str) and qual in held
        if listed:
            if resolves:
                problems.append(f"{name}:{line}: {m.group(0)} now exists; delete its planned entry")
        elif isinstance(held, str):
            problems.append(f"{name}:{line}: {path} {held}")
        elif not resolves:
            problems.append(f"{name}:{line}: {path} holds no {qual}")
    for name, tokens in planned.items():
        for token in tokens:
            if token not in cited.get(name, set()):
                problems.append(f"{name}: {token} is planned but no longer cited; delete it")
    return problems


def check_pins_exist(records: dict[str, str], source: Source) -> list[str]:
    """Check 3: each pin's commit exists and its file has the pinned line(s) there."""
    problems: list[str] = []
    for name, line, m in pin_tokens(records):
        path, sha = m.group("path"), m.group("sha")
        lo = int(m.group("lo"))
        hi = int(m.group("hi") or lo)
        where = f"{name}:{line}: {m.group(0)}"
        if len(sha) != 8:
            problems.append(f"{where} carries {len(sha)} hex digits, not 8")
        elif not source.commit_exists(sha):
            problems.append(f"{where} names a commit that does not exist")
        else:
            text = source.text_at(sha, path)
            if text is None:
                problems.append(f"{where} cites {path}, which is not in that commit")
            elif not 1 <= lo <= hi <= len(text.splitlines()):
                problems.append(f"{where} cites lines the file does not have at that commit")
    return problems


def check_quotes_unique(records: dict[str, str], source: Source) -> list[str]:
    """Check 4: a quoted line occurs exactly once in the file it is cited against."""
    problems: list[str] = []
    for name, line, m in quote_tokens(records, source):
        path, quote = m.group("path"), m.group("quote")
        text = source.head_text(path)
        if text is None:
            problems.append(f"{name}:{line}: {path} is not a tracked file")
            continue
        count = text.count(quote)
        if count == 0:
            problems.append(f"{name}:{line}: `{quote}` does not occur in {path}")
        elif count > 1:
            problems.append(f"{name}:{line}: `{quote}` occurs {count} times in {path}")
    return problems


def check_pins_are_listed(records: dict[str, str], known: dict[str, tuple[str, ...]]) -> list[str]:
    """Check 5: outside the edit record, the pins are exactly the ones in ``known``."""
    problems: list[str] = []
    seen: dict[str, set[str]] = {}
    for name, line, m in pin_tokens(records):
        if name == EDIT_RECORD:
            continue
        seen.setdefault(name, set()).add(m.group(0))
        if m.group(0) not in known.get(name, ()):
            problems.append(f"{name}:{line}: {m.group(0)} is a new pin; cite a symbol instead")
    for name, tokens in known.items():
        for token in tokens:
            if token not in seen.get(name, set()):
                problems.append(f"{name}: {token} is listed but no longer cited; delete it")
    return problems


def check_no_bare_members(records: dict[str, str]) -> list[str]:
    """Check 6: no line number follows a token outside backticks, unless the token is not Python.

    ``path.py::name`, :12`` leaves ``:12`` as a bare line number of a file the record cites
    by symbol. A token that is a repository path to a file of another language (a ``.rs``
    span) is never rewritten into a symbol, so its line numbers stay.
    """
    problems: list[str] = []
    for name, text in records.items():
        for run in _BARE_RUN.finditer(text):
            span = run.group("span")
            if _OTHER_PATH.fullmatch(span.split(None, 1)[0].split(":", 1)[0]):
                continue
            line = _lineno(text, run.start("run"))
            problems.extend(
                f"{name}:{line}: {member.group('number')} follows `{span}` "
                "as a bare line number; cite a symbol"
                for member in _BARE_MEMBER.finditer(run.group("run"))
            )
    return problems


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """Each run of non-blank lines as (number of its first line, its text)."""
    found: list[tuple[int, str]] = []
    first, lines = 0, []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not lines:
                first = number
            lines.append(line)
        elif lines:
            found.append((first, "\n".join(lines)))
            lines = []
    if lines:
        found.append((first, "\n".join(lines)))
    return found


def check_no_floating_line_numbers(records: dict[str, str]) -> list[str]:
    """Check 7: a code span holding only ``:N`` or ``:N-M`` does not follow a Python path.

    Such a span is a line number of the file the paragraph named last. When that file is
    Python the number goes stale like any ``path.py:N``, so the sentence must cite a symbol.
    A path to another kind of file (a record, a schema) keeps its line numbers.
    """
    problems: list[str] = []
    for name, text in records.items():
        for first, paragraph in _paragraphs(text):
            for span in _FLOATING.finditer(paragraph):
                before = list(_ANY_PATH.finditer(paragraph[: span.start()]))
                if before and before[-1].group("path").endswith(".py"):
                    line = first + paragraph.count("\n", 0, span.start())
                    problems.append(
                        f"{name}:{line}: `:{span.group('number')}` follows "
                        f"{before[-1].group('path')} as a bare line number; cite a symbol"
                    )
    return problems


def module_tokens(records: dict[str, str], source: Source) -> list[tuple[str, int, str]]:
    """Module citations as (record, line, path): a repository path to a Python file that is
    the whole content of a list item or of a table cell.

    A path that does not start at a top-level directory of the repository names no file by
    itself, so it is prose and not a citation.
    """
    roots = {path.split("/", 1)[0] for path in source.tracked() if "/" in path}
    found: list[tuple[str, int, str]] = []
    for name, text in records.items():
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("|"):
                matches = [_CELL.fullmatch(cell) for cell in line.strip().strip("|").split("|")]
            else:
                matches = [_ITEM.fullmatch(line)]
            found.extend(
                (name, number, m.group("path"))
                for m in matches
                if m is not None
                and "/" in m.group("path")
                and m.group("path").split("/", 1)[0] in roots
            )
    return found


def check_module_paths_exist(records: dict[str, str], source: Source) -> list[str]:
    """Check 8: every module citation names a file that is tracked at the head."""
    tracked = source.tracked()
    return [
        f"{name}:{line}: {path} is not a tracked file"
        for name, line, path in module_tokens(records, source)
        if path not in tracked
    ]


def _all_checks(
    records: dict[str, str],
    source: Source,
    known: dict[str, tuple[str, ...]],
    planned: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    return [
        *check_no_line_citations(records),
        *check_no_bare_members(records),
        *check_no_floating_line_numbers(records),
        *check_module_paths_exist(records, source),
        *check_symbols_exist(records, source, planned),
        *check_pins_exist(records, source),
        *check_quotes_unique(records, source),
        *check_pins_are_listed(records, known),
    ]


# A small tree: one module with a class, a nested function and a module-level binding,
# and an earlier commit of a different file.
REGISTRY = """\
\"\"\"Endpoint registry.\"\"\"

import os as operating_system

DEFAULT_TIMEOUT = 30


class Registry:
    def match(self, name):
        return self._entries.get(name)

    async def refresh(self):
        def reload_entry(entry):
            return entry

        return [reload_entry(e) for e in self._entries.values()]


def build_registry():
    return Registry()


def build_default():
    return Registry()
"""

OLD_REGISTRY = "\n".join(f"line {n}" for n in range(1, 13)) + "\n"

TREE = MemorySource(
    files={"pkg/registry.py": REGISTRY, "pkg/other.py": "VALUE = 1\n"},
    commits={"0123abcd": {"pkg/registry.py": OLD_REGISTRY}},
)

CLEAN = {
    "ADR-0001-registry.md": (
        "The registry resolves a name with `pkg/registry.py::Registry.match` and reloads\n"
        "through `pkg/registry.py::Registry.refresh.reload_entry`. The default timeout is\n"
        "`pkg/registry.py::DEFAULT_TIMEOUT`, the alias is `pkg/registry.py::operating_system`,\n"
        "and the builder is `pkg/registry.py` `def build_registry():`. The first release\n"
        "held it at `pkg/registry.py:3-7@0123abcd`.\n"
    ),
    EDIT_RECORD: "The edit touched `pkg/registry.py:9-12@0123abcd`.\n",
}
CLEAN_PINS = {"ADR-0001-registry.md": ("pkg/registry.py:3-7@0123abcd",)}


def _problems(doc: str, known: dict[str, tuple[str, ...]] | None = None) -> list[str]:
    records = {"ADR-0001-registry.md": doc}
    return _all_checks(records, TREE, known or {})


def test_clean_fixture_passes_every_check() -> None:
    assert _all_checks(CLEAN, TREE, CLEAN_PINS) == []


def test_module_path_alone_passes_every_check() -> None:
    doc = (
        "The whole module is `pkg/registry.py`, and the table row names it too:\n"
        "\n"
        "| Module | Role |\n"
        "| --- | --- |\n"
        "| `pkg/other.py` | the constants |\n"
    )
    assert _problems(doc) == []
    assert quote_tokens({"ADR-0001-registry.md": doc}, TREE) == []


def test_module_citation_in_a_list_naming_an_untracked_file_is_reported() -> None:
    doc = "The modules of the package:\n\n- `pkg/registry.py`\n- `pkg/missing.py`\n"
    problems = check_module_paths_exist({"ADR-0001-registry.md": doc}, TREE)
    assert problems == ["ADR-0001-registry.md:4: pkg/missing.py is not a tracked file"]


def test_module_citation_in_a_table_cell_naming_an_untracked_file_is_reported() -> None:
    doc = (
        "| Module | Role |\n"
        "| --- | --- |\n"
        "| `pkg/missing.py` | the constants |\n"
        "| `pkg/other.py` | the values |\n"
    )
    problems = check_module_paths_exist({"ADR-0001-registry.md": doc}, TREE)
    assert problems == ["ADR-0001-registry.md:3: pkg/missing.py is not a tracked file"]


def test_module_citations_naming_tracked_files_pass() -> None:
    doc = (
        "The modules of the package:\n"
        "\n"
        "- `pkg/registry.py`\n"
        "1. `pkg/other.py`\n"
        "\n"
        "| Module | Role |\n"
        "| --- | --- |\n"
        "| `pkg/other.py` | the values |\n"
        "| `pkg/registry.py` | the registry |\n"
    )
    records = {"ADR-0001-registry.md": doc}
    assert len(module_tokens(records, TREE)) == 4
    assert check_module_paths_exist(records, TREE) == []


def test_path_in_running_text_is_not_a_module_citation() -> None:
    doc = (
        "The retired `pkg/gone.py` is no longer used, and `registry/gone.py` was never a path.\n"
        "\n"
        "- `registry/gone.py`\n"
        "- `pkg/gone.py` held the old registry\n"
    )
    records = {"ADR-0001-registry.md": doc}
    assert module_tokens(records, TREE) == []
    assert check_module_paths_exist(records, TREE) == []


def test_line_range_after_a_python_path_in_the_paragraph_is_reported() -> None:
    doc = (
        "The registry resolves a name with `pkg/registry.py::Registry.match`, and\n"
        "`build_registry` at `:20-22` constructs it.\n"
    )
    problems = check_no_floating_line_numbers({"ADR-0001-registry.md": doc})
    assert problems == [
        "ADR-0001-registry.md:2: `:20-22` follows pkg/registry.py as a bare line number; "
        "cite a symbol"
    ]


def test_line_number_after_a_markdown_path_in_the_paragraph_passes() -> None:
    doc = (
        "- `docs/adr/ADR-0001-registry.md:8-12` says the registry is shared;\n"
        "  the same file at `:30-31` names its owner, and `pkg/registry.py::Registry` holds it.\n"
    )
    assert check_no_floating_line_numbers({"ADR-0001-registry.md": doc}) == []


def test_the_nearest_path_before_a_floating_line_number_decides() -> None:
    after_markdown = "See `pkg/registry.py::Registry`, then `docs/guide.md:3`, and `:9`.\n"
    after_python = "See `docs/guide.md:3`, then `pkg/registry.py::Registry`, and `:9`.\n"
    assert check_no_floating_line_numbers({"ADR-0001-registry.md": after_markdown}) == []
    assert len(check_no_floating_line_numbers({"ADR-0001-registry.md": after_python})) == 1


def test_a_path_in_an_earlier_paragraph_does_not_reach_a_floating_line_number() -> None:
    doc = (
        "The registry is `pkg/registry.py::Registry`.\n"
        "\n"
        "The same file at `:20-22` is a different paragraph, and a path after the number\n"
        "(`pkg/other.py::VALUE`) does not count either.\n"
        "\n"
        "A paragraph with no path at all cites `:5`.\n"
    )
    assert check_no_floating_line_numbers({"ADR-0001-registry.md": doc}) == []


def test_line_citation_without_a_pin_is_reported() -> None:
    doc = (
        "The registry matches a name (`pkg/registry.py:9`) and reloads (`pkg/registry.py:12-15`).\n"
    )
    problems = check_no_line_citations({"ADR-0001-registry.md": doc})
    assert len(problems) == 2
    assert "pkg/registry.py:9 " in problems[0]
    assert "pkg/registry.py:12-15 " in problems[1]


def test_pinned_citation_is_not_a_bare_line_citation() -> None:
    doc = "It sat at `pkg/registry.py:9@0123abcd` and at `pkg/registry.py:3-7@0123abcd`.\n"
    assert check_no_line_citations({"ADR-0001-registry.md": doc}) == []


def test_symbol_in_an_untracked_file_is_reported() -> None:
    doc = "The registry resolves a name with `pkg/missing.py::Registry.match`.\n"
    problems = check_symbols_exist({"ADR-0001-registry.md": doc}, TREE)
    assert problems == ["ADR-0001-registry.md:1: pkg/missing.py is not a tracked file"]


def test_symbol_the_file_does_not_hold_is_reported() -> None:
    doc = "The registry resolves a name with `pkg/registry.py::Registry.lookup`.\n"
    problems = check_symbols_exist({"ADR-0001-registry.md": doc}, TREE)
    assert problems == ["ADR-0001-registry.md:1: pkg/registry.py holds no Registry.lookup"]


def test_symbol_naming_a_method_without_its_class_is_reported() -> None:
    doc = "The registry resolves a name with `pkg/registry.py::match`.\n"
    assert check_symbols_exist({"ADR-0001-registry.md": doc}, TREE) != []


def test_symbol_in_a_file_that_does_not_parse_is_reported() -> None:
    broken = MemorySource(files={"pkg/broken.py": "def (:\n"})
    doc = "See `pkg/broken.py::run`.\n"
    problems = check_symbols_exist({"ADR-0001-registry.md": doc}, broken)
    assert problems == ["ADR-0001-registry.md:1: pkg/broken.py does not parse"]


def test_pin_naming_a_commit_that_does_not_exist_is_reported() -> None:
    doc = "The first release held it at `pkg/registry.py:3@deadbeef`.\n"
    problems = check_pins_exist({"ADR-0001-registry.md": doc}, TREE)
    assert len(problems) == 1
    assert "names a commit that does not exist" in problems[0]


def test_pin_beyond_the_end_of_the_file_is_reported() -> None:
    doc = "The first release held it at `pkg/registry.py:10-40@0123abcd`.\n"
    problems = check_pins_exist({"ADR-0001-registry.md": doc}, TREE)
    assert len(problems) == 1
    assert "cites lines the file does not have at that commit" in problems[0]


def test_pin_to_a_file_the_commit_does_not_hold_is_reported() -> None:
    doc = "The first release held it at `pkg/other.py:1@0123abcd`.\n"
    problems = check_pins_exist({"ADR-0001-registry.md": doc}, TREE)
    assert len(problems) == 1
    assert "is not in that commit" in problems[0]


def test_pin_with_a_short_hash_is_reported() -> None:
    doc = "The first release held it at `pkg/registry.py:3@0123ab`.\n"
    problems = check_pins_exist({"ADR-0001-registry.md": doc}, TREE)
    assert len(problems) == 1
    assert "not 8" in problems[0]


def test_quote_absent_from_the_file_is_reported() -> None:
    doc = "The builder is `pkg/registry.py` `def build_registrar():`.\n"
    problems = check_quotes_unique({"ADR-0001-registry.md": doc}, TREE)
    assert problems == [
        "ADR-0001-registry.md:1: `def build_registrar():` does not occur in pkg/registry.py"
    ]


def test_quote_occurring_twice_is_reported() -> None:
    doc = "The builders return one `pkg/registry.py` `return Registry()`.\n"
    problems = check_quotes_unique({"ADR-0001-registry.md": doc}, TREE)
    assert problems == [
        "ADR-0001-registry.md:1: `return Registry()` occurs 2 times in pkg/registry.py"
    ]


def test_quote_in_an_untracked_file_is_reported() -> None:
    doc = "The builder is `pkg/gone.py` `def build():`.\n"
    problems = check_quotes_unique({"ADR-0001-registry.md": doc}, TREE)
    assert problems == ["ADR-0001-registry.md:1: pkg/gone.py is not a tracked file"]


def test_path_pair_that_is_not_a_repository_path_is_not_a_quote() -> None:
    doc = "| `registry.py` | `Registry` |\n| `lib/registry.py` `Registry` | the registry class |\n"
    records = {"ADR-0001-registry.md": doc}
    assert quote_tokens(records, TREE) == []
    assert check_quotes_unique(records, TREE) == []


def test_planned_symbol_passes_while_its_code_is_absent() -> None:
    doc = "The new proof is named `pkg/registry.py::Registry.evict`.\n"
    planned = {"ADR-0001-registry.md": ("pkg/registry.py::Registry.evict",)}
    assert check_symbols_exist({"ADR-0001-registry.md": doc}, TREE) != []
    assert check_symbols_exist({"ADR-0001-registry.md": doc}, TREE, planned) == []


def test_planned_symbol_that_now_exists_is_reported() -> None:
    doc = "The registry resolves a name with `pkg/registry.py::Registry.match`.\n"
    planned = {"ADR-0001-registry.md": ("pkg/registry.py::Registry.match",)}
    problems = check_symbols_exist({"ADR-0001-registry.md": doc}, TREE, planned)
    assert len(problems) == 1
    assert "now exists" in problems[0]


def test_planned_symbol_that_is_no_longer_cited_is_reported() -> None:
    doc = "The registry resolves a name with `pkg/registry.py::Registry.match`.\n"
    planned = {"ADR-0001-registry.md": ("pkg/registry.py::Registry.evict",)}
    problems = check_symbols_exist({"ADR-0001-registry.md": doc}, TREE, planned)
    assert len(problems) == 1
    assert "is planned but no longer cited" in problems[0]


def test_pin_missing_from_the_list_is_reported() -> None:
    doc = "The first release held it at `pkg/registry.py:3-7@0123abcd`.\n"
    problems = check_pins_are_listed({"ADR-0001-registry.md": doc}, {})
    assert len(problems) == 1
    assert "is a new pin" in problems[0]


def test_pin_in_the_edit_record_needs_no_entry() -> None:
    doc = "The edit touched `pkg/registry.py:9-12@0123abcd`.\n"
    assert check_pins_are_listed({EDIT_RECORD: doc}, {}) == []


def test_listed_pin_that_is_no_longer_cited_is_reported() -> None:
    doc = "The registry resolves a name with `pkg/registry.py::Registry.match`.\n"
    problems = check_pins_are_listed({"ADR-0001-registry.md": doc}, CLEAN_PINS)
    assert len(problems) == 1
    assert "is listed but no longer cited" in problems[0]


def test_line_number_after_a_python_token_is_reported() -> None:
    doc = "The registry matches a name with `pkg/registry.py::Registry.match`, :12.\n"
    problems = check_no_bare_members({"ADR-0001-registry.md": doc})
    assert problems == [
        "ADR-0001-registry.md:1: :12 follows `pkg/registry.py::Registry.match` "
        "as a bare line number; cite a symbol"
    ]


def test_every_member_of_a_run_after_a_python_token_is_reported() -> None:
    doc = "It reloads (`pkg/registry.py::Registry.refresh`, :12; :15-18, :20) and returns.\n"
    problems = check_no_bare_members({"ADR-0001-registry.md": doc})
    assert [p.split(" ")[1] for p in problems] == [":12", ":15-18", ":20"]


def test_line_number_after_a_token_of_another_language_is_not_reported() -> None:
    doc = "The engine starts in `crates/engine/src/main.rs:3-9`, :12.\n"
    assert check_no_bare_members({"ADR-0001-registry.md": doc}) == []


def test_colon_and_digits_after_prose_are_not_a_bare_member() -> None:
    doc = "It sat at `pkg/registry.py:3@0123abcd`, and the ratio is 3, :1 to 2.\n"
    assert check_no_bare_members({"ADR-0001-registry.md": doc}) == []


def test_fixture_with_every_defect_is_reported_by_every_check() -> None:
    doc = (
        "A line `pkg/registry.py:9`, an untracked `pkg/missing.py::run`, "
        "a stray pin `pkg/registry.py:3@deadbeef`, "
        "a quote `pkg/registry.py` `return Registry()`, "
        "a member `pkg/registry.py::build_default`, :12.\n"
    )
    problems = _problems(doc)
    for fragment in (
        "cites a line number",
        "is not a tracked file",
        "names a commit that does not exist",
        "occurs 2 times",
        "is a new pin",
        "as a bare line number",
    ):
        assert any(fragment in p for p in problems), fragment


@pytest.fixture(scope="module")
def records() -> dict[str, str]:
    found = load_records(ADR_DIR)
    assert found, f"no records found under {ADR_DIR}"
    return found


@pytest.fixture(scope="module")
def git_source() -> GitSource:
    source = GitSource(REPO_ROOT)
    if not source.available():
        pytest.skip("not a git checkout")
    return source


def test_records_cite_no_code_by_line_number(records: dict[str, str]) -> None:
    problems = check_no_line_citations(records)
    assert not problems, "\n".join(problems)


def test_records_leave_no_bare_line_number_after_a_python_token(records: dict[str, str]) -> None:
    problems = check_no_bare_members(records)
    assert not problems, "\n".join(problems)


def test_every_symbol_citation_names_code_that_exists(
    records: dict[str, str], git_source: GitSource
) -> None:
    assert symbol_tokens(records), "no symbol citations found; the scan is broken"
    problems = check_symbols_exist(records, git_source, PLANNED_SYMBOLS)
    assert not problems, "\n".join(problems)


def test_every_pin_names_a_commit_that_has_the_line(
    records: dict[str, str], git_source: GitSource
) -> None:
    assert pin_tokens(records), "no pinned citations found; the scan is broken"
    if git_source.is_shallow():
        pytest.skip("a shallow checkout does not hold the commits the pins name")
    problems = check_pins_exist(records, git_source)
    assert not problems, "\n".join(problems)


def test_every_quote_occurs_once_in_its_file(
    records: dict[str, str], git_source: GitSource
) -> None:
    assert quote_tokens(records, git_source), "no quoted citations found; the scan is broken"
    problems = check_quotes_unique(records, git_source)
    assert not problems, "\n".join(problems)


def test_records_leave_no_floating_line_number_after_a_python_path(
    records: dict[str, str],
) -> None:
    problems = check_no_floating_line_numbers(records)
    assert not problems, "\n".join(problems)


def test_every_module_citation_names_a_tracked_file(
    records: dict[str, str], git_source: GitSource
) -> None:
    assert module_tokens(records, git_source), "no module citations found; the scan is broken"
    problems = check_module_paths_exist(records, git_source)
    assert not problems, "\n".join(problems)


def test_pins_outside_the_edit_record_are_listed(records: dict[str, str]) -> None:
    assert EDIT_RECORD in records, f"{EDIT_RECORD} is missing"
    problems = check_pins_are_listed(records, KNOWN_PINS)
    assert not problems, "\n".join(problems)
