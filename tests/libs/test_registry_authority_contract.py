# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Static and process-boundary gates for the ADR-0119 Registry authority."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY_MODULE = _ROOT / "lionagi" / "ln" / "types" / "registry.py"
_PUBLIC_SYMBOLS = (
    "AmbiguousRegistryOverrideError",
    "DuplicateRegistryOwnerError",
    "Registry",
    "RegistryCompositionError",
    "RegistryEntry",
    "RegistryFragment",
    "RegistryOverride",
    "RegistryOverrideRule",
    "RegistryRecord",
    "DuplicateRegistryKeyError",
)
_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "dataclasses",
        "typing",
        "typing_extensions",
        "lionagi.ln._structural",
        "lionagi.ln.types._sentinel",
        "lionagi.ln.types.base",
    }
)
_EXPECTED_MODULE_BINDINGS = frozenset({"__all__", "ItemT", "DefaultT", "_REGISTRY_INVARIANT_NAMES"})
_EXPECTED_FUNCTIONS = frozenset(
    {"_is_classvar_annotation", "_validate_label", "_validate_registry_value"}
)
_EXPECTED_CLASS_SURFACE = {
    "RegistryCompositionError": ({}, ()),
    "DuplicateRegistryOwnerError": ({}, ()),
    "DuplicateRegistryKeyError": ({}, ()),
    "AmbiguousRegistryOverrideError": ({}, ()),
    "RegistryEntry": ({"_validate": 1}, ("key", "value")),
    "RegistryFragment": ({"_validate": 1}, ("owner", "version", "items", "feature")),
    "RegistryRecord": (
        {"_validate": 1},
        ("entry", "owner", "fragment_version", "feature"),
    ),
    "RegistryOverrideRule": (
        {"_validate": 1, "matches": 1},
        (
            "key",
            "incumbent_owner",
            "incumbent_fragment_version",
            "replacement_owner",
            "replacement_fragment_version",
            "registry_version",
            "rule_version",
        ),
    ),
    "RegistryOverride": ({"_validate": 1}, ("rule", "displaced", "replacement")),
    "_RegistryType": ({"__new__": 1, "__setattr__": 1, "__delattr__": 1}, ()),
    "Registry": (
        {
            "__init__": 1,
            "_validate": 1,
            "compose": 1,
            "get": 3,
            "__getitem__": 1,
            "__contains__": 1,
            "keys": 1,
            "values": 1,
            "owner_of": 1,
            "with_updates": 1,
        },
        (
            "name",
            "fragments",
            "items",
            "overrides",
            "version",
            "override_rules",
            "_params_init_closed",
        ),
    ),
}


def _source() -> str:
    assert _REGISTRY_MODULE.exists(), (
        "ADR-0119 Registry authority must live at lionagi/ln/types/registry.py"
    )
    return _REGISTRY_MODULE.read_text()


def _tree(source: str | None = None) -> ast.Module:
    return ast.parse(source or _source(), filename=str(_REGISTRY_MODULE))


def _imported_module(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if node.level:
        package = ["lionagi", "ln", "types"]
        trim = node.level - 1
        if trim:
            package = package[:-trim]
        if node.module:
            package.extend(node.module.split("."))
        return (".".join(package),)
    return (node.module or "",)


def _symbol(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _symbol(node.value)
        return f"{owner}.{node.attr}" if owner else None
    return None


def _target_names(target: ast.AST) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Tuple | ast.List):
        return tuple(name for item in target.elts for name in _target_names(item))
    return ()


def _assignment_targets(node: ast.Assign | ast.AnnAssign) -> tuple[ast.AST, ...]:
    return tuple(node.targets) if isinstance(node, ast.Assign) else (node.target,)


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    return tuple(name for target in _assignment_targets(node) for name in _target_names(target))


def _contains_mutable_expression(node: ast.AST) -> bool:
    mutable_calls = {"WeakKeyDictionary", "defaultdict", "dict", "list", "set"}
    return any(
        isinstance(child, (ast.Dict, ast.DictComp, ast.List, ast.ListComp, ast.Set, ast.SetComp))
        or (
            isinstance(child, ast.Call)
            and (_symbol(child.func) or "").rsplit(".", 1)[-1] in mutable_calls
        )
        for child in ast.walk(node)
    )


def _closed_surface_violations(source: str) -> tuple[str, ...]:
    tree = _tree(source)
    violations: list[str] = []
    namespace_writes: Counter[str] = Counter()

    imported = {
        module
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for module in _imported_module(node)
    }
    if imported != _ALLOWED_IMPORTS:
        violations.append(
            f"imports differ: missing={sorted(_ALLOWED_IMPORTS - imported)}, "
            f"extra={sorted(imported - _ALLOWED_IMPORTS)}"
        )

    module_bindings = {
        name
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for name in _assignment_names(node)
    }
    if module_bindings != _EXPECTED_MODULE_BINDINGS:
        violations.append(f"module bindings differ: {sorted(module_bindings)}")

    for body_index, node in enumerate(tree.body):
        permitted_statement = isinstance(
            node,
            (
                ast.AnnAssign,
                ast.Assign,
                ast.ClassDef,
                ast.FunctionDef,
                ast.Import,
                ast.ImportFrom,
            ),
        ) or (
            body_index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        if not permitted_statement:
            violations.append(f"line {node.lineno}: unsupported module statement")
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        names = _assignment_names(node)
        value = node.value
        if any(not _target_names(target) for target in _assignment_targets(node)):
            violations.append(f"line {node.lineno}: indirect module assignment target")
        if value is not None and _contains_mutable_expression(value):
            violations.append(f"line {node.lineno}: mutable module assignment")
        for name in names:
            if name == "__all__" and not isinstance(value, ast.Tuple):
                violations.append("__all__ must be an immutable tuple")
            if name == "_REGISTRY_INVARIANT_NAMES" and not isinstance(value, ast.Tuple):
                violations.append("_REGISTRY_INVARIANT_NAMES must be an immutable tuple")
            if name in {"ItemT", "DefaultT"} and not (
                isinstance(value, ast.Call) and _symbol(value.func) == "TypeVar"
            ):
                violations.append(f"{name} must be a direct TypeVar declaration")

    functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    if functions != _EXPECTED_FUNCTIONS:
        violations.append(f"module functions differ: {sorted(functions)}")

    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    if set(classes) != set(_EXPECTED_CLASS_SURFACE):
        violations.append(f"classes differ: {sorted(classes)}")
    for class_name, (expected_methods, expected_fields) in _EXPECTED_CLASS_SURFACE.items():
        class_node = classes.get(class_name)
        if class_node is None:
            continue
        methods = Counter(
            item.name
            for item in class_node.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
        )
        if methods != Counter(expected_methods):
            violations.append(f"{class_name} methods differ: {dict(methods)}")
        fields = tuple(
            name
            for item in class_node.body
            if isinstance(item, ast.Assign | ast.AnnAssign)
            for name in _assignment_names(item)
        )
        if fields != expected_fields:
            violations.append(f"{class_name} fields differ: {fields}")
        for item in class_node.body:
            if not isinstance(item, ast.Assign | ast.AnnAssign) or item.value is None:
                continue
            if isinstance(
                item.value,
                (
                    ast.Call,
                    ast.Dict,
                    ast.DictComp,
                    ast.GeneratorExp,
                    ast.List,
                    ast.ListComp,
                    ast.Set,
                    ast.SetComp,
                ),
            ):
                violations.append(f"{class_name}:{item.lineno}: mutable/derived class binding")

    for node in ast.walk(tree):
        if isinstance(node, ast.Global | ast.Nonlocal):
            violations.append(f"line {node.lineno}: global/nonlocal mutation path")
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defaults = (*node.args.defaults, *(item for item in node.args.kw_defaults if item))
            for default in defaults:
                if _contains_mutable_expression(default):
                    violations.append(f"line {default.lineno}: mutable function default")
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets: tuple[ast.AST, ...]
            if isinstance(node, ast.Assign):
                targets = tuple(node.targets)
            else:
                targets = (node.target,)
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "namespace"
                ):
                    key = target.slice
                    if isinstance(key, ast.Constant) and type(key.value) is str:
                        namespace_writes[key.value] += 1
                    else:
                        violations.append(f"line {target.lineno}: dynamic namespace write")
                if isinstance(target, ast.Attribute) or (
                    isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute)
                ):
                    violations.append(f"line {target.lineno}: hidden attribute mutation path")
            value = (
                node.value if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr) else None
            )
            value_symbol = _symbol(value) if value is not None else None
            if (isinstance(value, ast.Name) and value.id == "namespace") or (
                value_symbol is not None and value_symbol.startswith("namespace.")
            ):
                violations.append(f"line {node.lineno}: namespace authority escape")
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "namespace":
                    violations.append(f"line {node.lineno}: namespace augmented mutation")
        if isinstance(node, ast.Name) and node.id in {
            "delattr",
            "eval",
            "exec",
            "getattr",
            "globals",
            "locals",
            "setattr",
            "vars",
        }:
            violations.append(f"line {node.lineno}: reflective authority reference")
        if isinstance(node, ast.Attribute) and _symbol(node) in {
            "object.__delattr__",
            "object.__setattr__",
            "type.__delattr__",
            "type.__setattr__",
        }:
            violations.append(f"line {node.lineno}: reflective mutation authority")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "namespace"
            and node.func.attr in {"clear", "pop", "popitem", "setdefault", "update", "__setitem__"}
        ):
            violations.append(f"line {node.lineno}: namespace mutator call")

    expected_namespace_writes = Counter(
        {
            "__slots__": 1,
            "_registry_classvars": 1,
            "_registry_invariants_locked": 1,
            "_registry_rules_locked": 1,
            "override_rules": 1,
        }
    )
    if namespace_writes != expected_namespace_writes:
        violations.append(f"namespace writes differ: {dict(namespace_writes)}")

    return tuple(violations)


def test_registry_authority_has_project_license_header():
    lines = _source().splitlines()

    assert lines[0].startswith("# Copyright (c) 2023-2026")
    assert lines[1] == "# SPDX-License-Identifier: Apache-2.0"


def test_registry_authority_has_one_closed_import_binding_and_method_surface():
    assert not (violations := _closed_surface_violations(_source())), violations


@pytest.mark.parametrize(
    "mutate",
    (
        lambda source: source + "\nimport importlib\n",
        lambda source: source + "\n_HIDDEN = {}\n",
        lambda source: source + "\ndef install(value):\n    return value\n",
        lambda source: source + "\nclass ExtraRegistryAuthority:\n    pass\n",
        lambda source: source.replace(
            "class Registry(Params, Generic[ItemT], metaclass=_RegistryType):\n",
            "class Registry(Params, Generic[ItemT], metaclass=_RegistryType):\n    _hidden = {}\n",
        ),
        lambda source: source.replace(
            "    def keys(self) -> tuple[str, ...]:\n",
            "    def install(self, value):\n"
            "        return value\n\n"
            "    def keys(self) -> tuple[str, ...]:\n",
        ),
        lambda source: source.replace(
            "def _validate_label(value: Any, field_name: str) -> None:\n",
            "def _validate_label(\n"
            "    value: Any, field_name: str, _cache: dict[str, Any] = {}\n"
            ") -> None:\n",
        ),
        lambda source: source.replace(
            "        rules = cls.override_rules\n",
            "        Registry._hidden_collector = {}\n        rules = cls.override_rules\n",
        ),
        lambda source: source + "\nglobals()['_HIDDEN'] = {}\n",
        lambda source: source + "\n(_HIDDEN,) = ({},)\n",
        lambda source: source + "\nglobals().update(_HIDDEN={})\n",
        lambda source: source.replace(
            "        rules = cls.override_rules\n",
            "        type.__setattr__(Registry, '_hidden_collector', {})\n"
            "        rules = cls.override_rules\n",
        ),
        lambda source: source.replace(
            "        rules = cls.override_rules\n",
            "        mutate = type.__setattr__\n"
            "        mutate(Registry, '_hidden_collector', {})\n"
            "        rules = cls.override_rules\n",
        ),
        lambda source: source.replace(
            "        return snapshot\n",
            "        object.__setattr__(snapshot, 'name', 'forged')\n        return snapshot\n",
        ),
        lambda source: source.replace(
            "        return snapshot\n",
            "        mutate = getattr(object, '__setattr__')\n"
            "        mutate(snapshot, 'name', 'forged')\n"
            "        return snapshot\n",
        ),
        lambda source: source.replace(
            '        namespace["override_rules"] = rules\n',
            '        namespace["_hidden"] = {}\n        namespace["override_rules"] = rules\n',
        ),
        lambda source: source.replace(
            '        namespace["override_rules"] = rules\n',
            '        namespace.update(_hidden={})\n        namespace["override_rules"] = rules\n',
        ),
        lambda source: source.replace(
            '        namespace["override_rules"] = rules\n',
            "        mutate = namespace.update\n"
            "        mutate(_hidden={})\n"
            '        namespace["override_rules"] = rules\n',
        ),
    ),
)
def test_closed_authority_gate_rejects_new_collectors_imports_and_mutators(mutate):
    assert _closed_surface_violations(mutate(_source()))


def test_registry_authority_has_no_global_mutable_collector_or_hidden_cache():
    tree = _tree()
    mutable_calls = {"dict", "list", "set", "defaultdict", "WeakKeyDictionary"}
    cache_decorators = {"cache", "lru_cache", "cached_property"}
    violations: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            value = node.value
        else:
            value = None
        if isinstance(value, ast.Dict | ast.List | ast.Set):
            violations.append(f"{_REGISTRY_MODULE.name}:{node.lineno}: mutable module value")
        elif (
            isinstance(value, ast.Call)
            and (_symbol(value.func) or "").rsplit(".", 1)[-1] in mutable_calls
        ):
            violations.append(
                f"{_REGISTRY_MODULE.name}:{node.lineno}: {_symbol(value.func)}() module value"
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (_symbol(target) or "").rsplit(".", 1)[-1] in cache_decorators:
                violations.append(
                    f"{_REGISTRY_MODULE.name}:{decorator.lineno}: hidden mutable cache"
                )

    assert not violations, (
        "Registry composition must derive a snapshot from explicit fragments; module-level "
        f"collectors and hidden caches are not authorities: {violations}"
    )


def test_registry_authority_exposes_no_mutating_registration_api():
    forbidden = {"register", "unregister", "update", "clear", "reset", "load_plugins"}
    violations: list[str] = []

    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden:
            violations.append(f"{_REGISTRY_MODULE.name}:{node.lineno}: {node.name}")

    assert not violations, (
        f"Registry snapshots are composed values, not mutable collectors: {violations}"
    )


def test_registry_types_are_public_foundation_exports():
    import lionagi.ln as ln
    import lionagi.ln.types as types

    missing = [
        f"{module.__name__}.{name}"
        for module in (types, ln)
        for name in _PUBLIC_SYMBOLS
        if not hasattr(module, name)
    ]

    assert not missing, f"Missing Registry foundation exports: {missing}"


def test_fresh_registry_import_does_not_pull_higher_or_optional_layers():
    source = """
import sys
from lionagi.ln.types import (
    AmbiguousRegistryOverrideError,
    DuplicateRegistryKeyError,
    DuplicateRegistryOwnerError,
    Registry,
    RegistryCompositionError,
    RegistryEntry,
    RegistryFragment,
    RegistryOverride,
    RegistryOverrideRule,
    RegistryRecord,
)
forbidden = (
    'pydantic', 'sqlalchemy', 'fastapi', 'aiosqlite',
    'lionagi.adapters', 'lionagi.models', 'lionagi.operations',
    'lionagi.providers', 'lionagi.session', 'lionagi.state', 'lionagi.studio',
)
loaded = sorted(name for name in sys.modules if name.startswith(forbidden))
if loaded:
    raise SystemExit('forbidden imports: ' + ', '.join(loaded))
"""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


_SEED_PROJECTION = """
import sys
from lionagi.ln import json_dumpb
from lionagi.ln.types import Registry, RegistryEntry, RegistryFragment
order = sys.argv[1].split(',')
fragment = RegistryFragment(
    owner='core',
    items=tuple(RegistryEntry(key=key, value=key.upper()) for key in order),
    version='1',
)
snapshot = Registry.compose(fragment, name='seeded', version='1')
sys.stdout.buffer.write(json_dumpb(snapshot.to_dict(mode='json')))
"""


def _projection_for_seed(seed: int, order: str = "first,second") -> bytes:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = str(seed)
    completed = subprocess.run(
        [sys.executable, "-c", _SEED_PROJECTION, order],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout


@pytest.mark.parametrize("seed", (0, 1, 17, 99991))
def test_registry_json_projection_is_identical_across_hash_seeds(seed):
    expected = _projection_for_seed(0)

    assert _projection_for_seed(seed) == expected


def test_registry_json_projection_preserves_intentional_item_order():
    forward = _projection_for_seed(0, "first,second")
    reversed_ = _projection_for_seed(0, "second,first")

    assert forward != reversed_
    assert forward.index(b"first") < forward.index(b"second")
    assert reversed_.index(b"second") < reversed_.index(b"first")
