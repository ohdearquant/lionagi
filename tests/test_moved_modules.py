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


def _inner_rows(row: _moved.Moved) -> list[_moved.Moved]:
    """Rows whose new package sits inside ``row``'s new package."""
    return [other for other in _moved.MOVED if other.new.startswith(row.new + ".")]


def _module_pairs() -> list[tuple[str, str]]:
    pairs = []
    for row in _moved.MOVED:
        package = importlib.import_module(row.new)
        names = [row.new]
        names += [info.name for info in pkgutil.walk_packages(package.__path__, row.new + ".")]
        # A package moved inside this one is paired under its own row, by the name it had.
        inner = tuple(other.new for other in _inner_rows(row))
        names = [n for n in names if not any(n == p or n.startswith(p + ".") for p in inner)]
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


def _run_fresh(code: str) -> str:
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed program
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return proc.stdout


# Two old names for one module, imported for the first time on two threads. The loader
# hooks pause each thread at a fixed point so the interleaving is the same on every run:
# the first thread is inside its load when the second starts its own. The second old name
# comes from an extra row for the same package, the shape later nested rows produce.
_TWO_OLD_NAMES_AT_ONCE = """
import importlib, sys, threading
from lionagi import _moved

row = _moved.MOVED[0]
twin = row.old + "_twin"
_moved.MOVED = _moved.MOVED + (_moved.Moved(twin, row.new, row.deprecated_in, row.removed_in),)
old, new = {old!r}, {new!r}
other = twin + old[len(row.old):]
moved = importlib.import_module(new)
importlib.import_module(old.rpartition(".")[0])
importlib.import_module(other.rpartition(".")[0])
assert old not in sys.modules and other not in sys.modules

first_inside, second_started, first_done = threading.Event(), threading.Event(), threading.Event()
create, execute = _moved._AliasLoader.create_module, _moved._AliasLoader.exec_module

def paused_create(loader, spec):
    module = create(loader, spec)
    if spec.name == other:
        second_started.set()
        assert first_done.wait(10)
    return module

def paused_execute(loader, module):
    if threading.current_thread().name == "first":
        first_inside.set()
        assert second_started.wait(10)
        execute(loader, module)
        first_done.set()
    else:
        execute(loader, module)

_moved._AliasLoader.create_module = paused_create
_moved._AliasLoader.exec_module = paused_execute
errors = []

def load(name):
    try:
        importlib.import_module(name)
    except BaseException as exc:
        errors.append(repr(exc))

first = threading.Thread(target=load, args=(old,), name="first")
second = threading.Thread(target=load, args=(other,), name="second")
first.start()
assert first_inside.wait(10)
second.start()
first.join(15)
second.join(15)
assert not first.is_alive() and not second.is_alive()
assert not errors, errors
assert sys.modules[old] is moved and sys.modules[other] is moved
print(moved.__spec__.name, moved.__name__)
"""


def test_two_old_names_imported_at_once_leave_the_module_spec_alone():
    old, new = next((o, n) for o, n in PAIRS if n.count(".") > _moved.MOVED[0].new.count("."))
    out = _run_fresh(_TWO_OLD_NAMES_AT_ONCE.format(old=old, new=new))
    assert out.split() == [new, new]


def test_an_old_package_not_yet_imported_is_described_as_a_package():
    row = _moved.MOVED[0]
    out = _run_fresh(
        "import importlib.util, pkgutil, sys\n"
        "import lionagi\n"
        f"assert {row.old!r} not in sys.modules\n"
        f"spec = importlib.util.find_spec({row.old!r})\n"
        f"new = importlib.util.find_spec({row.new!r})\n"
        "assert list(spec.submodule_search_locations) == list(new.submodule_search_locations)\n"
        f"old_bytes = pkgutil.get_data({row.old!r}, '__init__.py')\n"
        f"assert old_bytes and old_bytes == pkgutil.get_data({row.new!r}, '__init__.py')\n"
        "print('ok')\n"
    )
    assert out.split() == ["ok"]


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


def test_a_package_moved_inside_another_has_one_module_under_every_name():
    # lionhub.orchestration holds lionhub.orchestration.engines, so the old orchestration
    # prefix also names the engines ("lionagi.orchestration.engines"), a name that never
    # existed. It imports as the same module object: left to the normal import system it
    # would load a second copy of the engines from the aliased parent package's path.
    nested = [(outer, inner) for outer in _moved.MOVED for inner in _inner_rows(outer)]
    assert nested
    for outer, inner in nested:
        name = outer.old + inner.new[len(outer.new) :]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert importlib.import_module(name) is importlib.import_module(inner.new)


@pytest.mark.parametrize(("old", "new"), PAIRS, ids=[old for old, _ in PAIRS])
def test_no_old_module_is_still_on_disk(old, new):
    assert old.startswith("lionagi.")
    assert not _moved.still_on_disk(old), f"{old} exists in lionagi/ and in {new}"


def test_finder_steps_aside_for_a_module_still_at_its_old_path(tmp_path, monkeypatch):
    old = _moved.MOVED[0].old
    submodule = next(name for name, _ in PAIRS if name.startswith(old + "."))
    package = tmp_path.joinpath(*old.split(".")[1:])
    package.mkdir(parents=True)
    finder = next(f for f in sys.meta_path if getattr(f, "lionagi_moved", False))
    monkeypatch.setattr(_moved, "_LIONAGI_DIR", str(tmp_path))
    assert finder.find_spec(old) is not None
    (package / "__init__.py").write_text("")
    assert _moved.still_on_disk(old)
    assert finder.find_spec(old) is None
    assert finder.find_spec(submodule) is not None


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


def _module_of(path: Path) -> str | None:
    root = next((r for r in SOURCE_ROOTS if path.is_relative_to(r)), None)
    if root is None:
        return None
    parts = [root.name, *path.relative_to(root).with_suffix("").parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


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
            names = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            # A moved module keeps the logger name it had before the move, so logging
            # configuration keyed on that name still applies. Only its own old name or an
            # old package containing it; any other old name is a reference.
            if callee == "getLogger":
                own = _module_of(path)
                names = [
                    name
                    for name in names
                    if not (
                        own
                        and (new := _moved.resolve(name))
                        and (own == new or own.startswith(new + "."))
                    )
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
    assert [int(hit.split(":")[1]) for hit in hits] == [1, 2, 2, 3, 3, 4, 5], hits


def test_a_moved_module_may_keep_only_its_own_old_logger_name():
    row = _moved.MOVED[0]
    inside = ROOT / "lionhub" / "src" / Path(*row.new.split(".")) / "_probe.py"
    source = (
        f"logging.getLogger({row.old!r})\n"
        f"logging.getLogger({row.old + '._probe'!r})\n"
        f"logging.getLogger({row.old + '._other'!r})\n"
    )
    hits = _old_references(inside, source)
    assert [int(hit.split(":")[1]) for hit in hits] == [3], hits


# The packages under lionagi/ that may name lionhub: ones a later move takes out of the
# core. Every other package is core and never names it. The set must match what the
# tree does, so a package that stops naming lionhub is removed from it too.
LIONHUB_USERS_UNDER_LIONAGI = frozenset({"cli", "studio"})

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
