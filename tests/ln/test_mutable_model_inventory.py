"""Closed mutable-model declarations and statically resolvable hash authority.

The scan follows imports, reexports, lexical name/attribute aliases and declared
inheritance. It does not execute factories, decorators or reflective code, and
consumer references document audited boundaries rather than arbitrary dataflow.
Wildcard imports are rejected because they can conceal declared descendants.
Runtime container and mutation contracts cover the inventoried concrete types.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_INVENTORY = Path(__file__).with_name("data") / "mutable_model_inventory.json"
_ROOTS = frozenset(
    {"lionagi.ln.types.base.DataClass", "lionagi.models.hashable_model.HashableModel"}
)


@dataclass
class _Class:
    node: ast.ClassDef
    bases: tuple[str, ...]
    decorators: tuple[tuple[str, ast.expr], ...]


@dataclass
class _Scan:
    classes: dict[str, _Class]
    aliases: dict[str, str]
    symbols: set[str]
    writes: list[tuple[str, str]]


def _symbol(node: ast.AST, bindings: dict[str, str], module: str) -> str | None:
    if isinstance(node, ast.Name):
        fallback = (
            f"builtins.{node.id}" if node.id in {"object", "setattr"} else f"{module}.{node.id}"
        )
        return bindings.get(node.id, fallback)
    if isinstance(node, ast.Attribute):
        parent = _symbol(node.value, bindings, module)
        return f"{parent}.{node.attr}" if parent else None
    if isinstance(node, ast.Subscript):
        return _symbol(node.value, bindings, module)
    return None


def _canonical(symbol: str, aliases: dict[str, str]) -> str:
    seen = set()
    expanded = set()
    while symbol not in seen:
        seen.add(symbol)
        parts = symbol.split(".")
        for size in range(len(parts), 0, -1):
            prefix = ".".join(parts[:size])
            if prefix in aliases and aliases[prefix] != prefix:
                if prefix in expanded:
                    return symbol
                expanded.add(prefix)
                symbol = ".".join([aliases[prefix], *parts[size:]])
                break
        else:
            return symbol
    return symbol


def _none(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def _false(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Constant) and value.value is False


def _scan_sources(sources: dict[str, tuple[str, str]]) -> _Scan:
    result = _Scan({}, {}, set(), [])
    calls: list[tuple[str, str, str, str]] = []
    for module, (package, source) in sources.items():
        tree = ast.parse(source, filename=module)

        def walk(
            body: list[ast.stmt],
            bindings: dict[str, str],
            scope: str,
            class_name: str | None = None,
            closure: dict[str, str] | None = None,
        ) -> None:
            def bind(name: str, target: str) -> None:
                bindings[name] = target
                result.aliases[f"{scope}.{name}"] = target

            def assignment(target: ast.AST, value: ast.AST | None) -> None:
                if isinstance(target, ast.Name):
                    if class_name and target.id == "__hash__" and not _none(value):
                        result.writes.append((class_name, "assigned __hash__"))
                    bind(target.id, _symbol(value, bindings, module) or f"{scope}.{target.id}")
                elif isinstance(target, ast.Attribute) and target.attr == "__hash__":
                    owner = _symbol(target.value, bindings, module)
                    if owner and not _none(value):
                        result.writes.append((owner, "post-definition __hash__ assignment"))
                elif isinstance(target, (ast.Tuple, ast.List)):
                    values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
                    for index, child in enumerate(target.elts):
                        assignment(child, values[index] if index < len(values) else None)

            for node in body:
                if isinstance(node, ast.ImportFrom):
                    parts = package.split(".") if package else []
                    base = node.module or ""
                    if node.level:
                        if node.level > 1:
                            parts = parts[: -(node.level - 1)]
                        base = ".".join([*parts, *([base] if base else [])])
                    for item in node.names:
                        if item.name == "*":
                            raise ValueError(
                                f"{module}:{node.lineno}:{node.col_offset + 1}: wildcard import "
                                f"from {base!r} is unsupported in the closed mutable-model "
                                "inventory; use named imports"
                            )
                        bind(item.asname or item.name, f"{base}.{item.name}")
                elif isinstance(node, ast.Import):
                    for item in node.names:
                        local = item.asname or item.name.split(".")[0]
                        bind(local, item.name if item.asname else local)
                elif isinstance(node, ast.ClassDef):
                    fqn = f"{scope}.{node.name}"
                    bases = tuple(
                        symbol
                        for base in node.bases
                        if (symbol := _symbol(base, bindings, module)) is not None
                    )
                    decorators = tuple(
                        (symbol, decorator)
                        for decorator in node.decorator_list
                        if (
                            symbol := _symbol(
                                decorator.func if isinstance(decorator, ast.Call) else decorator,
                                bindings,
                                module,
                            )
                        )
                        is not None
                    )
                    result.classes[fqn] = _Class(node, bases, decorators)
                    result.symbols.add(fqn)
                    bind(node.name, fqn)
                    walk(node.body, bindings.copy(), fqn, fqn, closure or bindings.copy())
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fqn = f"{scope}.{node.name}"
                    result.symbols.add(fqn)
                    if class_name and node.name == "__hash__":
                        result.writes.append((class_name, "defined __hash__"))
                    bind(node.name, fqn)
                    local = (closure if class_name and closure is not None else bindings).copy()
                    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                    args.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg)
                    for arg in args:
                        local[arg.arg] = f"{fqn}.<locals>.{arg.arg}"
                    walk(node.body, local, f"{fqn}.<locals>")
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if node.value is not None:
                        for target in targets:
                            assignment(target, node.value)
                    inspect_calls(node, bindings)
                elif isinstance(node, ast.Delete):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "__hash__":
                            owner = _symbol(target.value, bindings, module)
                            if owner:
                                result.writes.append((owner, "deleted __hash__"))
                elif isinstance(node, ast.Expr):
                    inspect_calls(node, bindings)
                else:
                    # Inspect declarations in conditional, loop, try and match suites
                    # without pretending to decide which branch executes at runtime.
                    for _field, value in ast.iter_fields(node):
                        if isinstance(value, list):
                            statements = [item for item in value if isinstance(item, ast.stmt)]
                            if statements:
                                walk(statements, bindings.copy(), scope, class_name, closure)
                            for handler in value:
                                if isinstance(handler, (ast.ExceptHandler, ast.match_case)):
                                    walk(handler.body, bindings.copy(), scope, class_name, closure)

        def inspect_calls(node: ast.AST, bindings: dict[str, str]) -> None:
            for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
                function = _symbol(call.func, bindings, module)
                if function and len(call.args) == 3:
                    name = call.args[1]
                    if isinstance(name, ast.Constant) and name.value == "__hash__":
                        owner = _symbol(call.args[0], bindings, module)
                        if owner and not _none(call.args[2]):
                            calls.append((function, "builtins.setattr", owner, "setattr __hash__"))
                if function and call.args:
                    if any(
                        kw.arg in {"unsafe_hash", "frozen"} and not _false(kw.value)
                        for kw in call.keywords
                    ):
                        owner = _symbol(call.args[0], bindings, module)
                        if owner:
                            calls.append(
                                (
                                    function,
                                    "dataclasses.dataclass",
                                    owner,
                                    "post-definition dataclass hash generation",
                                )
                            )

        walk(tree.body, {}, module)
    for function, expected, owner, description in calls:
        if _canonical(function, result.aliases) == expected:
            result.writes.append((owner, description))
    return result


def _selected(scan: _Scan, roots: frozenset[str] = _ROOTS) -> frozenset[str]:
    selected = set(roots & scan.classes.keys())
    while True:
        extended = selected | {
            name
            for name, declaration in scan.classes.items()
            if any(_canonical(base, scan.aliases) in selected for base in declaration.bases)
        }
        if extended == selected:
            return frozenset(selected)
        selected = extended


def _generation(declaration: _Class, aliases: dict[str, str]) -> bool:
    if any(kw.arg == "frozen" and not _false(kw.value) for kw in declaration.node.keywords):
        return True
    for symbol, decorator in declaration.decorators:
        if _canonical(symbol, aliases) == "dataclasses.dataclass" and isinstance(
            decorator, ast.Call
        ):
            if any(
                kw.arg in {"unsafe_hash", "frozen"} and not _false(kw.value)
                for kw in decorator.keywords
            ):
                return True
    for node in declaration.node.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "model_config" for target in targets
            ):
                value = node.value
                if isinstance(value, ast.Call):
                    if any(kw.arg == "frozen" and not _false(kw.value) for kw in value.keywords):
                        return True
                if isinstance(value, ast.Dict):
                    if any(
                        isinstance(key, ast.Constant) and key.value == "frozen" and not _false(item)
                        for key, item in zip(value.keys, value.values)
                    ):
                        return True
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            for setting in node.body:
                if (
                    isinstance(setting, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "frozen"
                        for target in setting.targets
                    )
                    and not _false(setting.value)
                ):
                    return True
    return False


def _violations(scan: _Scan, expected: set[str]) -> list[str]:
    selected = _selected(scan)
    violations = []
    if selected != expected:
        violations.append(
            f"inventory drift: missing={sorted(expected - selected)}, unexpected={sorted(selected - expected)}"
        )
    for owner, description in scan.writes:
        owner = _canonical(owner, scan.aliases)
        if owner in selected:
            violations.append(f"{owner}: {description}")
    for name in selected:
        if _generation(scan.classes[name], scan.aliases):
            violations.append(f"{name}: generated hash or frozen configuration")
        ancestors = set()
        pending = list(scan.classes[name].bases)
        while pending:
            ancestor = _canonical(pending.pop(), scan.aliases)
            if ancestor in ancestors or ancestor not in scan.classes:
                continue
            ancestors.add(ancestor)
            pending.extend(scan.classes[ancestor].bases)
            if ancestor not in selected and _generation(scan.classes[ancestor], scan.aliases):
                violations.append(f"{name}: inherited generated hash authority from {ancestor}")
        for owner, description in scan.writes:
            owner = _canonical(owner, scan.aliases)
            if owner in ancestors - selected:
                violations.append(f"{name}: inherited hash authority from {owner} ({description})")
    return sorted(set(violations))


def _production_sources() -> dict[str, tuple[str, str]]:
    sources = {}
    for path in sorted((_ROOT / "lionagi").rglob("*.py")):
        parts = list(path.relative_to(_ROOT).with_suffix("").parts)
        package = parts[-1] == "__init__"
        if package:
            parts.pop()
        module = ".".join(parts)
        sources[module] = (module if package else module.rpartition(".")[0], path.read_text())
    return sources


@lru_cache(maxsize=1)
def _production() -> _Scan:
    return _scan_sources(_production_sources())


def _inventory() -> dict:
    return json.loads(_INVENTORY.read_text())


def test_production_mutable_model_inventory_is_closed():
    entries = _inventory()["types"]
    names = [entry["name"] for entry in entries]
    assert len(names) == len(set(names)) == 17
    assert _violations(_production(), set(names)) == []


def test_inventory_evidence_and_consumer_symbols_resolve():
    inventory = _inventory()
    scan = _production()
    consumers = inventory["consumers"]
    dataclasses = _selected(scan, frozenset({"lionagi.ln.types.base.DataClass"}))
    assert inventory["scope_limits"]
    for consumer in consumers.values():
        assert consumer["purpose"]
        assert consumer["symbol"] in scan.symbols
    for entry in inventory["types"]:
        assert entry["name"] in scan.classes
        assert entry["family"] in {"DataClass", "HashableModel"}
        assert (entry["family"] == "DataClass") == (entry["name"] in dataclasses)
        assert entry["runtime_fixture"] == entry["name"]
        evidence = entry["mutation_evidence"]
        assert evidence["kind"] in {"field", "private_state", "compatibility_subclass"}
        assert evidence["member"] and evidence["description"]
        assert entry["consumers"]
        assert set(entry["consumers"]) <= consumers.keys()
        assert entry["set_or_dict_key_consumers"] == []


def _sample(extra: str, other: dict[str, tuple[str, str]] | None = None) -> _Scan:
    return _scan_sources(
        {
            "lionagi.ln.types.base": (
                "lionagi.ln.types",
                "class DataClass:\n    __hash__ = None\n",
            ),
            "lionagi.models.hashable_model": (
                "lionagi.models",
                "class HashableModel:\n    __hash__ = None\n",
            ),
            "lionagi.models": ("lionagi.models", "from .hashable_model import HashableModel\n"),
            "sample": ("", extra),
            **(other or {}),
        }
    )


@pytest.mark.parametrize(
    "source, name",
    [
        (
            "from lionagi.models import HashableModel as H\nAlias = H\nclass Added(Alias): pass",
            "sample.Added",
        ),
        (
            "import lionagi.models as models\nclass Added(models.HashableModel): pass",
            "sample.Added",
        ),
        (
            "from lionagi.models import HashableModel\ndef make():\n    Alias = HashableModel\n    class Added(Alias): pass",
            "sample.make.<locals>.Added",
        ),
        (
            "def make():\n    from lionagi.models import HashableModel as Alias\n    class Added(Alias): pass",
            "sample.make.<locals>.Added",
        ),
        (
            "from lionagi.models import HashableModel\nclass Holder:\n    Alias = HashableModel\n    class Added(Alias): pass",
            "sample.Holder.Added",
        ),
        (
            "from lionagi.models import HashableModel\nclass Parent(HashableModel): pass\nclass Added(Parent): pass",
            "sample.Added",
        ),
    ],
)
def test_new_mutable_descendants_are_found_through_aliases(source, name):
    scan = _sample(source)
    assert name in _selected(scan)
    assert any(name in error for error in _violations(scan, set(_ROOTS)))


def test_cross_module_reexport_and_transitive_base_resolution():
    scan = _sample(
        "from facade import Alias\nclass Added(Alias): pass",
        {
            "foundation": (
                "",
                "from lionagi.models import HashableModel\nclass Parent(HashableModel): pass",
            ),
            "facade": ("", "from foundation import Parent as Alias"),
        },
    )
    assert {"foundation.Parent", "sample.Added"} <= _selected(scan)


@pytest.mark.parametrize("body", ["pass", "__hash__ = object.__hash__"])
@pytest.mark.parametrize("via_facade", [False, True], ids=["direct", "facade"])
def test_wildcard_imports_are_rejected_with_module_and_location(body, via_facade):
    if via_facade:
        source = "from facade import HashableModel\nclass Added(HashableModel):\n    " + body
        other = {"facade": ("", "\n\nfrom lionagi.models import *\n")}
        module = "facade"
    else:
        source = "\n\nfrom lionagi.models import *\nclass Added(HashableModel):\n    " + body
        other = None
        module = "sample"
    with pytest.raises(ValueError, match=rf"^{module}:3:1: wildcard import .* unsupported"):
        _sample(source, other)


def test_prefix_alias_resolution_terminates_on_cycles_and_expanding_aliases():
    assert _canonical("a.Member", {"a": "b", "b": "a"}) == "a.Member"
    assert _canonical("a.Member", {"a": "a.Nested"}) == "a.Nested.Member"


def test_missing_declaration_and_extra_entry_are_inventory_drift():
    scan = _sample("")
    assert _violations(scan, {*_ROOTS, "sample.Missing"}) == [
        "inventory drift: missing=['sample.Missing'], unexpected=[]"
    ]


def test_decorator_reexport_alias_cannot_generate_a_hash():
    scan = _sample(
        "from lionagi.models import HashableModel\nfrom helpers import decorate\n"
        "@decorate(unsafe_hash=True)\nclass Known(HashableModel): pass",
        {"helpers": ("", "from dataclasses import dataclass as decorate")},
    )
    assert _violations(scan, {*_ROOTS, "sample.Known"}) == [
        "sample.Known: generated hash or frozen configuration"
    ]


def test_hash_setter_reexport_is_resolved_after_all_modules_are_scanned():
    scan = _sample(
        "from lionagi.models import HashableModel\nfrom helpers import setter\n"
        "class Known(HashableModel): pass\nsetter(Known, '__hash__', object.__hash__)",
        {"helpers": ("", "from builtins import setattr as setter")},
    )
    assert _violations(scan, {*_ROOTS, "sample.Known"}) == ["sample.Known: setattr __hash__"]


def test_multiple_inheritance_cannot_supply_an_unlisted_hash_authority():
    scan = _sample("""
from lionagi.models import HashableModel
class IdentityMixin:
    __hash__ = object.__hash__
class Known(IdentityMixin, HashableModel): pass
""")
    assert any(
        "inherited hash authority from sample.IdentityMixin" in error
        for error in _violations(scan, {*_ROOTS, "sample.Known"})
    )


def test_multiple_inheritance_cannot_supply_a_generated_hash_authority():
    scan = _sample("""
from dataclasses import dataclass
from lionagi.models import HashableModel
@dataclass(unsafe_hash=True)
class IdentityMixin: pass
class Known(IdentityMixin, HashableModel): pass
""")
    assert _violations(scan, {*_ROOTS, "sample.Known"}) == [
        "sample.Known: inherited generated hash authority from sample.IdentityMixin"
    ]


@pytest.mark.parametrize(
    "suite",
    [
        "if flag:\n    {}",
        "if flag:\n    pass\nelse:\n    {}",
        "try:\n    pass\nexcept ImportError:\n    {}",
        "try:\n    pass\nfinally:\n    {}",
        "with context:\n    {}",
        "for _ in ():\n    {}",
        "match flag:\n    case 1:\n        {}",
        "match flag:\n    case 1:\n        pass\n    case _:\n        {}",
    ],
    ids=["if", "else", "except", "finally", "with", "for", "case", "later-case"],
)
def test_a_descendant_declared_inside_any_suite_is_inventoried(suite):
    declaration = "class Hidden(HashableModel): pass"
    scan = _sample("from lionagi.models import HashableModel\n" + suite.format(declaration))
    assert _violations(scan, {*_ROOTS}) == [
        "inventory drift: missing=[], unexpected=['sample.Hidden']"
    ]


@pytest.mark.parametrize(
    "body",
    [
        "    def __hash__(self): return 1",
        "    __hash__ = object.__hash__",
        "    __hash__: object = object.__hash__",
        "    model_config = {'frozen': True}",
        "    model_config = ConfigDict(frozen=True)",
        "    class Config:\n        frozen = True",
    ],
)
def test_existing_member_cannot_gain_hash_authority(body):
    scan = _sample("from lionagi.models import HashableModel\nclass Known(HashableModel):\n" + body)
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors and all("sample.Known" in error for error in errors)


@pytest.mark.parametrize(
    "decoration",
    [
        "@dc(unsafe_hash=True)\nclass Known(HashableModel): pass",
        "@dc(frozen=True)\nclass Known(HashableModel): pass",
        "class Known(HashableModel, frozen=True): pass",
    ],
)
def test_generated_hash_authority_is_rejected(decoration):
    scan = _sample(
        "from lionagi.models import HashableModel\nfrom dataclasses import dataclass as dc\n"
        + decoration
    )
    assert _violations(scan, {*_ROOTS, "sample.Known"})


@pytest.mark.parametrize(
    "assignment",
    [
        "Known.__hash__ = object.__hash__",
        "Alias = Known\nAlias.__hash__: object = object.__hash__",
        "setattr(Known, '__hash__', object.__hash__)",
        "setter = setattr\nsetter(Known, '__hash__', object.__hash__)",
        "del Known.__hash__",
        "from dataclasses import dataclass\ndataclass(Known, unsafe_hash=True)",
        "def change():\n    Alias = Known\n    Alias.__hash__ = object.__hash__",
    ],
)
def test_post_definition_hash_authority_is_rejected(assignment):
    scan = _sample(
        "from lionagi.models import HashableModel\nclass Known(HashableModel): pass\n" + assignment
    )
    assert _violations(scan, {*_ROOTS, "sample.Known"})


def test_unrelated_hashes_and_shadowed_names_are_not_mutable_family_authority():
    scan = _sample("""
from lionagi.models import HashableModel
class Known(HashableModel):
    __hash__ = None
class Unrelated:
    __hash__ = object.__hash__
def shadow(HashableModel):
    class Local(HashableModel):
        __hash__ = object.__hash__
Alias = HashableModel
Alias = object
class Other(Alias):
    __hash__ = object.__hash__
Known.__hash__ = None
""")
    assert _violations(scan, {*_ROOTS, "sample.Known"}) == []
