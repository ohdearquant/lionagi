"""Every module that moved out of lionagi keeps its old import path until its removal release.

The table is ``lionagi._moved.MOVED``. For every module under each moved package these
tests check that the old name imports, that it is the same module object as the new
name, that it warns once and points the warning at the importing code, that the
removal release is still ahead of this version, that no source file in lionagi or
lionhub imports an old name, and that the CHANGELOG names every move.
"""

from __future__ import annotations

import ast
import importlib
import pickle
import pkgutil
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from lionagi import _moved
from lionagi.version import __version__

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "lionagi", ROOT / "lionhub" / "src" / "lionhub")


def _release(text: str) -> tuple[int, ...]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", text)
    assert match, f"not a release number: {text!r}"
    return tuple(int(part) for part in match.groups())


def _module_pairs() -> list[tuple[str, str]]:
    pairs = []
    for row in _moved.MOVED:
        package = importlib.import_module(row.new)
        names = [row.new]
        names += [info.name for info in pkgutil.walk_packages(package.__path__, row.new + ".")]
        pairs += [(row.old + name[len(row.new) :], name) for name in sorted(names)]
    return pairs


PAIRS = _module_pairs()


def test_the_table_has_moves_and_every_package_has_modules():
    assert _moved.MOVED
    for row in _moved.MOVED:
        assert any(new == row.new for _, new in PAIRS), row
    assert len(PAIRS) > len(_moved.MOVED)


@pytest.mark.parametrize(("old", "new"), PAIRS, ids=[old for old, _ in PAIRS])
def test_old_name_is_the_new_module_and_warns_once(old, new):
    parent = old.rpartition(".")[0]
    if _moved.resolve(parent) is not None:
        importlib.import_module(parent)
    sys.modules.pop(old, None)
    target = importlib.import_module(new)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = importlib.import_module(old)
        again = importlib.import_module(old)

    assert module is target
    assert again is target
    assert sys.modules[old] is target
    assert target.__spec__.name == new
    assert target.__name__ == new

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, [str(w.message) for w in deprecations]
    message = str(deprecations[0].message)
    row = _moved._row(old)
    assert old in message and new in message and row.removed_in in message
    assert deprecations[0].filename == __file__


def test_import_statement_warning_points_at_the_importing_line():
    old, new = PAIRS[0]
    sys.modules.pop(old, None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        exec(f"import {old}", {"__name__": "user_code"})  # noqa: S102
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert deprecations[0].filename == "<string>"
    assert sys.modules[old] is sys.modules[new]


def test_objects_are_shared_and_pickles_naming_the_old_path_load():
    checked = {row.old: 0 for row in _moved.MOVED}
    for old, new in PAIRS:
        importlib.import_module(old)
        assert sys.modules[old] is sys.modules[new]
        module = sys.modules[new]
        for name, obj in vars(module).items():
            if name.startswith("_") or getattr(obj, "__module__", None) != new:
                continue
            if getattr(obj, "__qualname__", None) != name:
                continue
            # Protocol 0 writes module names as text lines, so the rename is byte-safe.
            payload = pickle.dumps(obj, protocol=0).replace(new.encode(), old.encode())
            assert old.encode() in payload
            assert pickle.loads(payload) is obj  # noqa: S301
            checked[_moved._row(old).old] += 1
    assert all(checked.values()), checked


def test_reload_through_the_old_name_keeps_the_real_spec():
    old, new = PAIRS[0]
    module = importlib.import_module(old)
    assert importlib.reload(module) is sys.modules[new]
    assert module.__spec__.name == new
    assert module.__name__ == new


def _lionhub_modules_after(statement: str) -> list[str]:
    code = (
        f"import sys; {statement}; "
        "print(sorted(m for m in sys.modules if m == 'lionhub' or m.startswith('lionhub.')))"
    )
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed statement
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return ast.literal_eval(proc.stdout.strip().splitlines()[-1])


def test_importing_lionagi_imports_nothing_from_lionhub():
    # Old names resolve on demand, so the core alone loads no lionhub module. The second
    # import shows the same probe does see lionhub modules once an old name is imported.
    assert _lionhub_modules_after("import lionagi") == []
    old = _moved.MOVED[0].old
    assert _moved.resolve(old) in _lionhub_modules_after(f"import lionagi, {old}")


def test_finder_is_installed_once_and_first_on_the_import_path():
    finders = [f for f in sys.meta_path if getattr(f, "lionagi_moved", False)]
    assert len(finders) == 1
    _moved.install()
    assert [f for f in sys.meta_path if getattr(f, "lionagi_moved", False)] == finders
    assert sys.meta_path.index(finders[0]) < next(
        i for i, f in enumerate(sys.meta_path) if f is importlib.machinery.PathFinder
    )


def test_names_outside_the_table_are_left_to_the_normal_import_system():
    first = _moved.MOVED[0]
    assert _moved.resolve(first.old + "x") is None
    assert _moved.resolve("lionagi") is None
    assert _moved.resolve(first.old) == first.new
    assert _moved.resolve(first.old + ".a.b") == first.new + ".a.b"
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(first.old + ".no_such_module")


@pytest.mark.parametrize(("old", "new"), PAIRS, ids=[old for old, _ in PAIRS])
def test_no_old_module_is_still_on_disk(old, new):
    assert old.startswith("lionagi.")
    assert not _moved.still_on_disk(old), f"{old} exists in lionagi/ and in {new}"


def test_finder_steps_aside_for_a_module_still_at_its_old_path(tmp_path, monkeypatch):
    old = _moved.MOVED[0].old
    package = tmp_path.joinpath(*old.split(".")[1:])
    package.mkdir(parents=True)
    finder = next(f for f in sys.meta_path if getattr(f, "lionagi_moved", False))
    monkeypatch.setattr(_moved, "_LIONAGI_DIR", str(tmp_path))
    assert finder.find_spec(old) is not None
    (package / "__init__.py").write_text("")
    assert _moved.still_on_disk(old)
    assert finder.find_spec(old) is None
    assert finder.find_spec(old + ".x") is not None


@pytest.mark.parametrize("row", _moved.MOVED, ids=[row.old for row in _moved.MOVED])
def test_removal_release_is_ahead_of_this_version(row):
    # Fails on the release that reaches ``removed_in``: delete the row and its old
    # callers' last chance is over, as the CHANGELOG said it would be.
    assert _release(row.deprecated_in) < _release(row.removed_in)
    assert _release(__version__) < _release(row.removed_in), (
        f"{row.old} was due for removal in {row.removed_in}; delete its row"
    )


def _absolute(module: str | None, level: int, path: Path) -> str | None:
    if level == 0:
        return module
    root = next((r for r in SOURCE_ROOTS if path.is_relative_to(r)), None)
    if root is None:
        return None
    package = [root.name, *path.relative_to(root).with_suffix("").parts][:-1]
    package = package[: len(package) - (level - 1)]
    return ".".join([*package, module] if module else package)


def _old_references(path: Path, source: str | None = None) -> list[str]:
    if source is None:
        source = path.read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module, node.level, path)
            if base:
                names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call):
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            # Logger names are kept from before the move on purpose.
            if callee == "getLogger":
                continue
            names = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
        found += [
            f"{path.relative_to(ROOT)}:{node.lineno}: {name}"
            for name in names
            if _moved.resolve(name) is not None
        ]
    return found


def test_no_source_file_imports_an_old_name():
    files = [p for root in SOURCE_ROOTS for p in root.rglob("*.py")]
    assert any(p.name == "_moved.py" for p in files)
    assert any(p.is_relative_to(SOURCE_ROOTS[1]) for p in files)
    offenders = [hit for p in files if p.name != "_moved.py" for hit in _old_references(p)]
    assert offenders == []


def test_the_reference_check_sees_each_form_of_old_reference():
    old = _moved.MOVED[0].old
    leaf = old.rpartition(".")[2]
    probe = ROOT / "lionagi" / "cli" / "_probe.py"
    source = (
        f"import {old}\n"
        f"from {old} import x\n"
        f"from ..{leaf} import y\n"
        f"importlib.import_module({old!r})\n"
        f"logging.getLogger({old!r})\n"
    )
    hits = _old_references(probe, source)
    assert [int(hit.split(":")[1]) for hit in hits] == [1, 2, 2, 3, 3, 4], hits


# The packages under lionagi/ that may name lionhub: ones a later move takes out of the
# core. Every other package is core and never names it. The set must match what the
# tree does, so a package that stops naming lionhub is removed from it too.
LIONHUB_USERS_UNDER_LIONAGI = frozenset({"cli", "engines"})

_LIONHUB_NAME = re.compile(r"lionhub(\.[A-Za-z_][A-Za-z0-9_]*)*")


def _lionhub_names(path: Path, source: str | None = None) -> list[str]:
    """Every import of lionhub, or string naming a lionhub module, in ``path``."""
    text = path.read_text(encoding="utf-8") if source is None else source
    hits = []
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]
        else:
            continue
        hits += [f"{path}:{node.lineno}: {n}" for n in names if _LIONHUB_NAME.fullmatch(n)]
    return hits


def test_only_packages_that_move_later_name_lionhub():
    lionagi = SOURCE_ROOTS[0]
    users = {}
    # _moved.py names lionhub as data: the table of new paths it serves old names from.
    for path in lionagi.rglob("*.py"):
        if path.name != "_moved.py" and _lionhub_names(path):
            top = path.relative_to(lionagi).parts[0]
            users.setdefault(top.removesuffix(".py"), []).append(str(path.relative_to(ROOT)))
    assert set(users) == LIONHUB_USERS_UNDER_LIONAGI, users


def test_the_lionhub_check_sees_each_form_of_reference():
    probe = ROOT / "lionagi" / "cli" / "_probe.py"
    source = (
        "import lionhub.orchestration\n"
        "from lionhub.orchestration import patterns\n"
        "importlib.import_module('lionhub.orchestration.patterns')\n"
        "PREFIXES = ('lionhub.', 'lionhubx.orchestration')\n"
    )
    hits = _lionhub_names(probe, source)
    assert [int(hit.split(":")[1]) for hit in hits] == [1, 2, 3], hits


def test_changelog_names_every_move():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for row in _moved.MOVED:
        line = f"| `{row.old}` | `{row.new}` | {row.removed_in} |"
        assert line in changelog, row
