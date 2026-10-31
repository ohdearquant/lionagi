"""Every module that moved out of lionagi keeps its old import path until its removal release.

The table is ``lionagi._moved.MOVED``. For every module under each moved package these
tests check that the old name imports, that it is the same module object as the new
name, that it warns once and points the warning at the importing code, that the
removal release is still ahead of this version, that data naming a moved Element
class by either path loads in a fresh process and saves under the new path, that the
class-path allow-list adds only the moved packages' new prefixes to ``lionagi.``, that no
source file in lionagi or lionhub imports an old name, and that the CHANGELOG names every
move.
"""

from __future__ import annotations

import ast
import importlib
import json
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
        search_path = getattr(package, "__path__", None)  # a lone module has no submodules
        if search_path is not None:
            names += [info.name for info in pkgutil.walk_packages(search_path, row.new + ".")]
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


def _moved_element_classes() -> list[tuple[str, type]]:
    from lionagi.protocols.generic.element import Element

    found = []
    for old, new in PAIRS:
        for obj in vars(importlib.import_module(new)).values():
            if isinstance(obj, type) and issubclass(obj, Element) and obj.__module__ == new:
                found.append((old, obj))
    return found


ELEMENT_CLASSES = _moved_element_classes()


def _work_form(cls: type) -> object:
    field_spec = importlib.import_module(cls.__module__).FieldSpec
    # The field is given in its saved form, as a form loaded from storage holds it, so the
    # nested element's metadata names its class too.
    field = field_spec(name="amount", type="int").to_dict()
    return cls(title="intake", fields={"amount": field})


# One instance of every Element subclass a moved module defines. Element data names its
# class by module path (metadata["lion_class"]), so each one is loaded below from data
# naming either path.
SAMPLES = {
    "lionhub.orchestration.work.form.FieldSpec": lambda cls: cls(name="amount", type="int"),
    "lionhub.orchestration.work.form.WorkForm": _work_form,
}

# Runs in a fresh interpreter, where nothing from lionhub is imported until the data
# names it: a process that already imported the class resolves it from sys.modules and
# would pass whatever the allow-list says.
_LOAD_IN_FRESH_PROCESS = """
import importlib, json, sys
from lionagi.protocols.generic.element import Element
text, how, target = sys.argv[1:4]
cold = not any(name == "lionhub" or name.startswith("lionhub.") for name in sys.modules)
try:
    if how == "from_dict":
        obj = Element.from_dict(json.loads(text))
    else:
        module, _, name = target.rpartition(".")
        obj = getattr(importlib.import_module(module), name).model_validate(json.loads(text))
    result = {"class": type(obj).__module__ + "." + type(obj).__qualname__, "json": obj.to_json()}
except Exception as exc:
    result = {"error": type(exc).__name__ + ": " + str(exc)}
result["cold"] = cold
result["lionhub_after"] = sorted(
    name for name in sys.modules if name == "lionhub" or name.startswith("lionhub.")
)
print(json.dumps(result))
"""


def _load_in_fresh_process(text: str, how: str, target: str = "") -> dict:
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed script
        [sys.executable, "-c", _LOAD_IN_FRESH_PROCESS, text, how, target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_moved_element_class_has_a_sample():
    assert {f"{cls.__module__}.{cls.__qualname__}" for _, cls in ELEMENT_CLASSES} == set(SAMPLES)


def test_the_form_sample_nests_an_element_that_names_its_class():
    # The test below rewrites every class path in the data, the nested one included, and
    # expects each to be saved under the new path again.
    cls = next(cls for _, cls in ELEMENT_CLASSES if cls.__qualname__ == "WorkForm")
    saved = json.loads(SAMPLES[f"{cls.__module__}.WorkForm"](cls).to_json())
    assert saved["fields"]["amount"]["metadata"]["lion_class"] == f"{cls.__module__}.FieldSpec"


@pytest.mark.parametrize(
    ("old", "cls"), ELEMENT_CLASSES, ids=[cls.__qualname__ for _, cls in ELEMENT_CLASSES]
)
def test_moved_element_data_loads_in_a_fresh_process_under_either_path(old, cls):
    new = cls.__module__
    path = f"{new}.{cls.__qualname__}"
    text = SAMPLES[path](cls).to_json()
    expected = json.loads(text)
    old_text = text.replace(f'"{new}.', f'"{old}.')
    assert old_text.count(f'"{old}.') == text.count(f'"{new}.') > 0

    for data in (text, old_text):
        result = _load_in_fresh_process(data, "from_dict")
        assert result["cold"], "lionhub was imported before the data was read"
        assert "error" not in result, result["error"]
        assert result["class"] == path
        assert json.loads(result["json"]) == expected

    # Metadata written before the move names the old path; the class accepts it as its own.
    result = _load_in_fresh_process(old_text, "model_validate", path)
    assert "error" not in result, result["error"]
    assert json.loads(result["json"]) == expected


@pytest.mark.parametrize(
    "module",
    [
        # Names that share letters with an allowed prefix but not the prefix itself.
        "lionhubx.orchestration.work.form",
        "lionhub_x.orchestration.work.form",
        # lionhub code outside the moved packages: the top-level package and a sibling.
        "lionhub",
        "lionhub.canon.work.form",
    ],
)
def test_a_path_outside_the_allowed_prefixes_is_refused_in_a_fresh_process(module):
    _, cls = ELEMENT_CLASSES[0]
    new = cls.__module__
    text = SAMPLES[f"{new}.{cls.__qualname__}"](cls).to_json()
    outside = text.replace(f'"{new}.', f'"{module}.')
    assert outside != text

    result = _load_in_fresh_process(outside, "from_dict")
    assert result["cold"]
    assert "register_type_prefix" in result["error"], result
    assert result["lionhub_after"] == []


@pytest.mark.parametrize(
    "path",
    [
        "lionhub..orchestration.work.form.WorkForm",
        "lionhub.orchestration..work.form.WorkForm",
        ".lionhub.orchestration.work.form.WorkForm",
    ],
)
def test_a_malformed_lionhub_path_is_refused_before_anything_is_imported(path):
    _, cls = ELEMENT_CLASSES[0]
    name = f"{cls.__module__}.{cls.__qualname__}"
    text = SAMPLES[name](cls).to_json()
    malformed = text.replace(json.dumps(name), json.dumps(path), 1)
    assert malformed != text

    result = _load_in_fresh_process(malformed, "from_dict")
    assert result["cold"]
    assert "not a dotted class path" in result["error"], result
    assert result["lionhub_after"] == []


def test_the_allow_list_adds_only_the_moved_packages_to_lionagi():
    # Data may import a module under a moved package's new prefix (ADR-0001 Amendment 2)
    # because the finder serves every module under that prefix by its old lionagi name,
    # which "lionagi." already admits; the old-name tests above import each one that way.
    # Any other prefix would reach code no lionagi name reaches and needs its own decision.
    from lionagi.ln._utils import _DEFAULT_ALLOWED_PREFIXES

    added = _DEFAULT_ALLOWED_PREFIXES - {"lionagi."}
    moved = {f"{row.new}." for row in _moved.MOVED}
    assert added
    assert added <= moved
    assert all(any(prefix.startswith(a) for a in added) for prefix in moved)


# A module that moved on its own (one file, not a package) is a row of the table too. Its old
# name sits inside a package that stays in lionagi/, so the finder must serve it while that
# package is still on disk.
MOVED_MODULES = (
    ("lionagi.providers._provider_errors", "lionagi._provider_errors"),
    ("lionagi.providers._secret_resolution", "lionagi.service.accounts._secret_resolution"),
)
_MOVED_MODULE_IDS = [old for old, _ in MOVED_MODULES]


@pytest.mark.parametrize(("old", "new"), MOVED_MODULES, ids=_MOVED_MODULE_IDS)
def test_an_old_module_name_warns_once_and_is_the_new_module(old, new):
    sys.modules.pop(old, None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = importlib.import_module(old)
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, [str(w.message) for w in deprecations]
    assert new in str(deprecations[0].message)
    assert deprecations[0].filename == __file__
    assert module is importlib.import_module(new)
    assert sys.modules[old] is module


# What each moved module offers its callers. Naming some of it keeps the identity check
# below from passing on a module that defines nothing.
MOVED_MODULE_PUBLIC_NAMES = {
    "lionagi._provider_errors": {"ProviderError", "EmissionError"},
    "lionagi.service.accounts._secret_resolution": {
        "SecretLookupResolution",
        "fill_declared_secrets",
        "lookup_one_name",
        "resolve_secret_lookup_config",
    },
}


@pytest.mark.parametrize(("old", "new"), MOVED_MODULES, ids=_MOVED_MODULE_IDS)
def test_every_public_object_of_a_moved_module_is_the_same_object_under_the_old_name(old, new):
    module = importlib.import_module(new)
    public = [
        name
        for name, obj in vars(module).items()
        if not name.startswith("_") and callable(obj) and getattr(obj, "__module__", None) == new
    ]
    assert MOVED_MODULE_PUBLIC_NAMES[new] <= set(public)
    for name in public:
        namespace: dict = {}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            exec(f"from {old} import {name}", namespace)  # noqa: S102
        assert namespace[name] is getattr(module, name), name


def test_a_moved_module_keeps_the_logger_name_it_had():
    module = importlib.import_module("lionagi.service.accounts._secret_resolution")
    assert module.logger.name == "lionagi.providers._secret_resolution"


@pytest.mark.parametrize(("old", "new"), MOVED_MODULES, ids=_MOVED_MODULE_IDS)
def test_an_old_module_name_is_served_while_its_old_package_stays_on_disk(old, new):
    assert _moved.still_on_disk("lionagi.providers")
    assert not _moved.still_on_disk(old)
    assert _moved.resolve(old) == new
    # Names beside the moved module, and the package around it, did not move.
    assert _moved.resolve(old + "x") is None
    assert _moved.resolve("lionagi.providers") is None
    assert _moved.resolve("lionagi.providers.errors") is None
    finder = next(f for f in sys.meta_path if getattr(f, "lionagi_moved", False))
    spec = finder.find_spec(old)
    assert spec is not None
    assert spec.submodule_search_locations is None
    assert finder.find_spec("lionagi.providers") is None


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


# `python -m <old name>` runs a package's __main__ without importing it, through the
# loader's code (runpy, as `-m` does). MCP client configs start the server this way.
_SCRIPT_ROWS = [
    row
    for row in _moved.MOVED
    if (ROOT / "lionhub" / "src" / Path(*row.new.split(".")) / "__main__.py").is_file()
]


@pytest.mark.parametrize("row", _SCRIPT_ROWS, ids=lambda row: row.old)
def test_an_old_package_with_a_main_module_still_runs_as_a_script(row):
    out = _run_fresh(
        "import importlib, runpy, sys\n"
        f"g = runpy.run_module({row.old!r}, run_name='not_main')\n"
        f"new = importlib.import_module({row.new!r})\n"
        f"print(g['__spec__'].name, g['__package__'], sys.modules[{row.old!r}] is new)\n"
    )
    assert out.split() == [f"{row.old}.__main__", row.old, "True"]


def test_some_moved_package_has_a_main_module():
    assert _SCRIPT_ROWS


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


# An old and a new name of one submodule, imported for the first time on two threads at once,
# forty times over. The old name reaches the new package through its top, and the package's
# __init__ imports the submodule, so without the retry in _moved most rounds fail with
# importlib's deadlock error on the old-name thread. The retry covers that thread only.
# importlib can also fail the thread importing the new name, on the new package's lock: it
# does the same for a package and its submodule imported at once with no old name involved,
# so the new-name thread may see that one error and no other.
_OLD_AND_NEW_AT_ONCE = """
import importlib, json, sys, threading, warnings
import lionagi

warnings.simplefilter("ignore")
old, new = {old!r}, {new!r}
packages = ({old_package!r}, {new_package!r})
errors = dict(old=[], new=[])

def load(barrier, name, side):
    barrier.wait()
    try:
        importlib.import_module(name)
    except BaseException as exc:
        errors[side].append([type(exc).__name__, str(exc)])

for _ in range(40):
    for name in [m for m in sys.modules if m in packages or m.startswith(tuple(p + "." for p in packages))]:
        del sys.modules[name]
    failed = len(errors["old"]) + len(errors["new"])
    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=load, args=(barrier, n, side)) for n, side in ((new, "new"), (old, "old"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads)
    if len(errors["old"]) + len(errors["new"]) == failed:
        assert sys.modules[old] is sys.modules[new]
print(json.dumps(errors))
"""


def test_an_old_and_a_new_name_imported_at_once_both_load():
    row = _moved.MOVED[0]
    # The package's __init__ imports this submodule, which is what lets the two imports cross.
    old, new = row.old + ".patterns", row.new + ".patterns"
    assert (old, new) in PAIRS
    out = _run_fresh(
        _OLD_AND_NEW_AT_ONCE.format(old=old, new=new, old_package=row.old, new_package=row.new)
    )
    errors = json.loads(out)
    assert errors["old"] == [], errors
    package_lock = f"_ModuleLock({row.new!r})"
    assert all(kind == "_DeadlockError" and package_lock in text for kind, text in errors["new"]), (
        errors
    )


def test_a_python_without_the_private_deadlock_error_still_reports_import_errors():
    # The retry names a private importlib class. Without it, a failed import of a moved module
    # must still raise its own error rather than one from evaluating the except clause.
    out = _run_fresh(
        "import importlib._bootstrap as b\n"
        "del b._DeadlockError\n"
        "from lionagi import _moved\n"
        "assert _moved._DEADLOCK_ERROR == ()\n"
        "try:\n"
        "    _moved._import_new('lionhub.no_such_module')\n"
        "except ModuleNotFoundError as exc:\n"
        "    print(type(exc).__name__, exc.name)\n"
    )
    assert out.split() == ["ModuleNotFoundError", "lionhub.no_such_module"], out


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


# Wave 2 moves these out of the core (D6), so the rest of lionagi must not import them. Logger
# names such as "lionagi.cli.warn" are strings, not imports, and D5 keeps them.
WAVE_2 = ("cli", "mcp", "studio")
_WAVE_2_NAME = re.compile(rf"lionagi\.({'|'.join(WAVE_2)})(\.[A-Za-z_][A-Za-z0-9_]*)*")
_IMPORT_CALLS = frozenset({"import_module", "__import__"})


def _call_argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((kw.value for kw in node.keywords if kw.arg == keyword), None)


def _literal_import_name(node: ast.Call, call: str, package: tuple[str, ...]) -> str | None:
    """The module a literal ``import_module``/``__import__`` call names, resolved like importlib."""
    name = _call_argument(node, 0, "name")
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return None
    if not name.value.startswith("."):
        return name.value
    if call != "import_module":
        # ``__import__`` takes ``globals`` second and its level as an argument of its own.
        return None
    # A relative name resolves against the ``package`` argument; ``__package__`` is this module's.
    anchor = _call_argument(node, 1, "package")
    if isinstance(anchor, ast.Constant) and isinstance(anchor.value, str):
        parts = tuple(anchor.value.split("."))
    elif isinstance(anchor, ast.Name) and anchor.id == "__package__":
        parts = package
    else:
        return None
    level = len(name.value) - len(name.value.lstrip("."))
    return ".".join((*parts[: len(parts) - level + 1], *filter(None, [name.value[level:]])))


def _wave_2_imports(path: Path, source: str | None = None) -> list[str]:
    """Every import of a wave-2 package in ``path``: absolute, relative, or a literal dynamic one."""
    text = path.read_text(encoding="utf-8") if source is None else source
    package = path.relative_to(ROOT).with_suffix("").parts[:-1]
    tree = ast.parse(text, filename=str(path))
    # ``from importlib import import_module as load`` makes ``load(...)`` an import call too.
    calls = {name: name for name in _IMPORT_CALLS} | {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "importlib"
        for alias in node.names
        if alias.name in _IMPORT_CALLS and alias.asname
    }
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join((*base, *filter(None, [node.module])))
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) in calls
        ):
            call = calls[getattr(node.func, "attr", getattr(node.func, "id", None))]
            names = [n for n in [_literal_import_name(node, call, package)] if n]
        else:
            continue
        hits += [f"{path}:{node.lineno}: {n}" for n in names if _WAVE_2_NAME.fullmatch(n)][:1]
    return hits


def test_the_core_imports_no_package_that_moves_in_wave_2():
    lionagi = SOURCE_ROOTS[0]
    hits = [
        hit
        for path in sorted(lionagi.rglob("*.py"))
        if path.relative_to(lionagi).parts[0] not in WAVE_2
        for hit in _wave_2_imports(path)
    ]
    assert hits == [], hits


def test_the_wave_2_check_sees_each_form_of_import():
    probe = ROOT / "lionagi" / "state" / "_probe.py"
    source = (
        "from lionagi.cli._runs import RunDir\n"
        "from ..mcp import server\n"
        "importlib.import_module('lionagi.studio.app')\n"
        "import lionagi.cli\n"
        "from lionagi import studio\n"
        "from .. import cli\n"
        "importlib.import_module(name='lionagi.mcp')\n"
        "importlib.import_module('.cli._runs', package='lionagi')\n"
        "importlib.import_module('..studio', __package__)\n"
        "from importlib import import_module as load\n"
        "load('lionagi.cli')\n"
        "WARN = 'lionagi.cli.warn'\n"
        "from lionagi.client import x\n"
        "from . import cli\n"
        "importlib.import_module('.client', package='lionagi')\n"
        "__import__('.cli', globals())\n"
        "importlib.import_module(name=module_name)\n"
    )
    hits = _wave_2_imports(probe, source)
    assert sorted(int(hit.split(":")[1]) for hit in hits) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11], hits


def test_changelog_names_every_move():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for row in _moved.MOVED:
        line = f"| `{row.old}` | `{row.new}` | {row.removed_in} |"
        assert line in changelog, row
