"""Design records cite code by symbol, or by a pin that never moves.

A citation of the form ``path.py:120`` goes stale as soon as a line is inserted above
line 120, so every such change would have to renumber the records. The records cite
code in one of three forms instead:

* a symbol, ``path.py::Class.method`` (or ``path.py::NAME`` for a module-level binding),
  which names the code and does not move;
* a quote, a ``path.py`` span followed by a short code span holding one line of that file,
  which must occur exactly once in the file;
* a pin, ``path.py:N@<hash8>``, for code that was rewritten or removed so that no symbol
  holds it any more: the commit fixes the file, so line N never moves.

Five checks keep that true. Each is a plain function over the record texts and a
``Source`` (the tracked files, their current text, and the file text at a commit), so a
small in-memory fixture can carry exactly one defect and a test can require the check to
report it. The tests over the real records use the repository checkout as the source.
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
# only while the code it points at has no symbol at the head. A new pin fails
# test_pins_outside_the_edit_record_are_listed, and a pin that is gone from its record
# fails it too, so the list can only shrink: when a cited symbol returns, replace the pin
# with the symbol and delete its entry here.
KNOWN_PINS: dict[str, tuple[str, ...]] = {
    "ADR-0066-li-mcp-v2-verb-surface.md": (
        "lionagi/cli/kill.py:378@95b559a4",
        "lionagi/cli/kill.py:383@95b559a4",
        "lionagi/cli/kill.py:392-418@95b559a4",
        "lionagi/cli/kill.py:488-498@95b559a4",
    ),
    "ADR-0069-reactive-flow-steering-and-recovery.md": (
        "lionagi/cli/orchestrate/_checkpoint.py:29@72461356",
        "lionagi/cli/orchestrate/_manifest.py:333@72461356",
    ),
    "ADR-0109-mirrored-session-idleness-is-not-completion.md": (
        "lionagi/cli/mirror.py:42@9caaceb3",
        "lionagi/state/lifecycle/callbacks.py:45@ee4cfee3",
    ),
    "ADR-0117-normalized-progression-membership-and-online-cutover.md": (
        "lionagi/cli/state.py:247@93932753",
        "lionagi/cli/state.py:284@93932753",
        "lionagi/state/db.py:3445@6fe53aff",
    ),
    "ADR-0130-per-request-input-ceiling-and-context-management-past-it.md": (
        "lionagi/providers/anthropic/messages.py:988@2a8b69c9",
    ),
    "ADR-0131-operator-decisions-require-a-launcher-issued-decision-capability.md": (
        "lionagi/studio/cli.py:274@124b1419",
        "tests/studio/test_hosted_hardening.py:967@124b1419",
    ),
    "ADR-0135-bounded-opt-in-session-and-run-listing.md": (
        "lionagi/state/schema_migrations.py:10@35a520d4",
        "lionagi/state/schema_migrations.py:270@6fe53aff",
        "tests/apps_studio_server/test_session_listing_projection.py:188@35a520d4",
    ),
    "ADR-0136-moving-non-core-packages-into-lionhub.md": (
        "lionagi/_auto.py:157-161@7c434920",
        "lionagi/_auto.py:175@7c434920",
        "lionagi/_auto.py:180@7c434920",
        "lionagi/casts/emission.py:401@c4abb363",
        "lionagi/casts/emission.py:422-434@c4abb363",
        "lionagi/casts/emission.py:437@c4abb363",
        "lionagi/lndl/extract.py:12-27@c4abb363",
        "lionagi/operations/flow.py:1126@c4abb363",
        "lionagi/operations/flow.py:1162@c4abb363",
        "lionagi/operations/flow.py:1228@c4abb363",
        "lionagi/operations/flow.py:1306@c4abb363",
        "lionagi/operations/lndl_middle/lndl_middle.py:16@0e6f1979",
        "lionagi/operations/parse/parse.py:315@c4abb363",
        "lionagi/providers/_secret_resolution.py:101@c4abb363",
        "lionagi/session/signal.py:280@c4abb363",
        "lionagi/state/provenance.py:19@1e7be920",
        "lionagi/studio/scheduler/subprocess.py:279@c4abb363",
    ),
    "ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md": (
        "lionagi/cli/mirror.py:21@ddb46ce9",
        "lionagi/studio/services/schedule_declaration.py:482@8c119ecc",
        "tests/test_moved_modules.py:669@8c119ecc",
    ),
    "ADR-0139-cli-package-location-and-console-script-ownership.md": (
        "lionagi/_auto.py:157@7c434920",
        "lionagi/_auto.py:172@7c434920",
        "lionagi/_auto.py:223@7c434920",
        "lionagi/cli/_runs.py:1913@11c965a2",
        "lionagi/cli/_runs.py:2131@11c965a2",
        "lionagi/cli/engine.py:29@11c965a2",
        "lionagi/cli/engine.py:62@11c965a2",
        "lionagi/cli/kill.py:43@83260865",
        "lionagi/cli/kill.py:508@83260865",
        "lionagi/cli/kill.py:794@83260865",
        "lionagi/cli/mirror.py:111@20073afe",
        "lionagi/cli/mirror.py:21@fdaea538",
        "lionagi/cli/mirror.py:25@20073afe",
        "lionagi/cli/mirror.py:854@20073afe",
        "lionagi/cli/orchestrate/__init__.py:394@fdaea538",
        "lionagi/cli/orchestrate/__init__.py:425@fdaea538",
        "lionagi/cli/orchestrate/_checkpoint.py:227@fdaea538",
        "lionagi/cli/status.py:262@fdaea538",
        "lionagi/cli/status.py:37@fdaea538",
        "lionagi/cli/status.py:55@fdaea538",
        "lionagi/cli/team.py:42@fdaea538",
        "lionagi/studio/scheduler/subprocess.py:279@fdaea538",
    ),
}

# Symbols a Proposed record asks for that do not exist yet, by record file. A name is allowed
# here only while the head holds no such code; once it exists the entry fails and must go.
PLANNED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md": (
        "tests/packaging/test_studio_backend_boundary.py::"
        "test_fresh_wheel_schedule_validate_and_export_without_backend",
    ),
}

_PATH = r"[\w./-]+\.py"
# ``path.py:N``, ``path.py:N-M``, either with an optional ``@<hash>`` pin.
_LINE = re.compile(
    rf"(?<![\w./-])(?P<path>{_PATH}):(?P<lo>\d+)(?:[-–](?P<hi>\d+))?(?:@(?P<sha>[0-9a-f]+))?"
)
# ``path.py::Class.method`` and ``path.py::NAME``.
_SYMBOL = re.compile(
    rf"(?<![\w./-])(?P<path>{_PATH})::(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
)
# `path.py` followed by one backticked line of that file.
_QUOTE = re.compile(rf"`(?P<path>{_PATH})` `(?P<quote>[^`\n]+)`")


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


def quote_tokens(
    records: dict[str, str], source: Source
) -> list[tuple[str, int, re.Match[str]]]:
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


def check_pins_are_listed(
    records: dict[str, str], known: dict[str, tuple[str, ...]]
) -> list[str]:
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


def _all_checks(
    records: dict[str, str],
    source: Source,
    known: dict[str, tuple[str, ...]],
    planned: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    return [
        *check_no_line_citations(records),
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


def test_line_citation_without_a_pin_is_reported() -> None:
    doc = (
        "The registry matches a name (`pkg/registry.py:9`) "
        "and reloads (`pkg/registry.py:12-15`).\n"
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
    doc = (
        "| `registry.py` | `Registry` |\n"
        "| `lib/registry.py` `Registry` | the registry class |\n"
    )
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


def test_fixture_with_every_defect_is_reported_by_every_check() -> None:
    doc = (
        "A line `pkg/registry.py:9`, an untracked `pkg/missing.py::run`, "
        "a stray pin `pkg/registry.py:3@deadbeef`, "
        "a quote `pkg/registry.py` `return Registry()`.\n"
    )
    problems = _problems(doc)
    for fragment in (
        "cites a line number",
        "is not a tracked file",
        "names a commit that does not exist",
        "occurs 2 times",
        "is a new pin",
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


def test_pins_outside_the_edit_record_are_listed(records: dict[str, str]) -> None:
    assert EDIT_RECORD in records, f"{EDIT_RECORD} is missing"
    problems = check_pins_are_listed(records, KNOWN_PINS)
    assert not problems, "\n".join(problems)
