"""Closed production contract for ADR-0119 structural identity owners."""

from __future__ import annotations

import ast
import importlib
from collections.abc import Hashable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_PACKAGE = _ROOT / "lionagi"
_AUTHORITIES = frozenset(
    {
        "lionagi.ln.types.base.Params",
        "lionagi.ln.types.base.Meta",
        "lionagi.ln.types.spec.Spec",
    }
)
_EXPECTED = frozenset(
    {
        *_AUTHORITIES,
        "lionagi.casts.pattern.Pattern",
        "lionagi.casts.pattern.Mode",
        "lionagi.casts.pattern.Role",
        "lionagi.ln._async_call.AlcallParams",
        "lionagi.ln._async_call.BcallParams",
        "lionagi.ln._to_list.ToListParams",
        "lionagi.ln.fuzzy._fuzzy_match.FuzzyMatchKeysParams",
        "lionagi.ln.types.registry.Registry",
        "lionagi.ln.types.registry.RegistryEntry",
        "lionagi.ln.types.registry.RegistryFragment",
        "lionagi.ln.types.registry.RegistryOverride",
        "lionagi.ln.types.registry.RegistryOverrideRule",
        "lionagi.ln.types.registry.RegistryRecord",
        "lionagi.models.field_model.FieldModel",
        "lionagi.operations.types.MorphParam",
        "lionagi.operations.types.ChatParam",
        "lionagi.operations.types.RunParam",
        "lionagi.operations.types.InterpretParam",
        "lionagi.operations.types.ParseParam",
        "lionagi.operations.types.ActionParam",
    }
)

_MUTABLE_MODEL_BASES = frozenset(
    {
        "lionagi.ln.types.base.DataClass",
        "lionagi.models.hashable_model.HashableModel",
    }
)


@dataclass(frozen=True)
class _MutableModelInventoryEntry:
    mutation_evidence: str
    key_consumers: tuple[str, ...] = ()
    dedup_consumers: tuple[str, ...] = ()


# Machine-readable closure for mutable runtime-model identity. A new descendant must name its
# surface and every set/dict-key or transient dedup consumer before CI accepts it.
_MUTABLE_MODEL_INVENTORY = {
    "lionagi.ln.types.base.DataClass": _MutableModelInventoryEntry("mutable dataclass substrate"),
    "lionagi.protocols.messages.message.MessageContent": _MutableModelInventoryEntry(
        "assignment and revision-tracked list/dict fields"
    ),
    "lionagi.protocols.messages.action_request.ActionRequestContent": (
        _MutableModelInventoryEntry("message field assignment")
    ),
    "lionagi.protocols.messages.action_response.ActionResponseContent": (
        _MutableModelInventoryEntry("message field assignment")
    ),
    "lionagi.protocols.messages.assistant_response.AssistantResponseContent": (
        _MutableModelInventoryEntry("message field assignment")
    ),
    "lionagi.protocols.messages.instruction.InstructionContent": (
        _MutableModelInventoryEntry("message field and tracked-container assignment")
    ),
    "lionagi.protocols.messages.system.SystemContent": _MutableModelInventoryEntry(
        "message field assignment"
    ),
    "lionagi.models.hashable_model.HashableModel": _MutableModelInventoryEntry(
        "Pydantic validate_assignment"
    ),
    "lionagi.models.operable_model.OperableModel": _MutableModelInventoryEntry(
        "add_field/update_field/remove_field"
    ),
    "lionagi.models.schema_model.SchemaModel": _MutableModelInventoryEntry(
        "Pydantic validate_assignment"
    ),
    "lionagi.libs.schema.function_to_schema.FunctionSchema": _MutableModelInventoryEntry(
        "Pydantic validate_assignment"
    ),
    "lionagi.operations.ReAct.utils.ReActAnalysis": _MutableModelInventoryEntry(
        "Pydantic validate_assignment"
    ),
    "lionagi.operations.ReAct.utils.Analysis": _MutableModelInventoryEntry(
        "Pydantic validate_assignment"
    ),
    "lionagi.operations.fields.Instruct": _MutableModelInventoryEntry(
        "Pydantic validate_assignment",
        dedup_consumers=("lionagi.operations.fields._validate_listable_field",),
    ),
    "lionagi.operations.fields.Reason": _MutableModelInventoryEntry(
        "Pydantic validate_assignment",
        dedup_consumers=("lionagi.operations.fields._validate_listable_field",),
    ),
    "lionagi.operations.fields.ActionRequestModel": _MutableModelInventoryEntry(
        "Pydantic validate_assignment",
        dedup_consumers=("lionagi.operations.fields._validate_listable_field",),
    ),
    "lionagi.operations.fields.ActionResponseModel": _MutableModelInventoryEntry(
        "Pydantic validate_assignment",
        dedup_consumers=("lionagi.operations.fields._validate_listable_field",),
    ),
}


@dataclass(frozen=True)
class _Module:
    name: str
    package: str
    tree: ast.Module
    bindings: dict[str, str]


@dataclass(frozen=True)
class _Class:
    fqn: str
    module: _Module
    node: ast.ClassDef
    bases: tuple[str, ...]
    bindings: dict[str, str | None]


def _module_name(path: Path) -> tuple[str, str]:
    relative = path.relative_to(_ROOT).with_suffix("")
    parts = list(relative.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    name = ".".join(parts)
    package = name if is_package else name.rpartition(".")[0]
    return name, package


def _from_base(package: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    parts = package.split(".") if package else []
    if node.level > 1:
        parts = parts[: -(node.level - 1)]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _module_bindings(
    module_name: str,
    package: str,
    tree: ast.Module,
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            base = _from_base(package, node)
            for imported in node.names:
                if imported.name == "*":
                    continue
                local = imported.asname or imported.name
                bindings[local] = f"{base}.{imported.name}" if base else imported.name
        elif isinstance(node, ast.Import):
            for imported in node.names:
                local = imported.asname or imported.name.split(".", 1)[0]
                bindings[local] = imported.name if imported.asname else local
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings[node.name] = f"{module_name}.{node.name}"

    def resolve_assignment(value: ast.AST) -> str | None:
        if isinstance(value, ast.Name):
            return bindings.get(value.id, f"{module_name}.{value.id}")
        if isinstance(value, ast.Attribute):
            parent = resolve_assignment(value.value)
            return f"{parent}.{value.attr}" if parent else None
        return None

    # A module-level alias is a normal way to spell a base class. Import-only
    # resolution would let generated dataclass equality silently re-enter via
    # ``Alias = Params``.
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target = resolve_assignment(node.value)
            if target is None:
                continue
            for assignment_target in node.targets:
                if isinstance(assignment_target, ast.Name):
                    bindings[assignment_target.id] = target
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None and (target := resolve_assignment(node.value)) is not None:
                bindings[node.target.id] = target
    return bindings


def _symbol(
    node: ast.AST,
    module: _Module,
    bindings: dict[str, str | None] | None = None,
) -> str | None:
    if isinstance(node, ast.Name):
        if bindings is not None and node.id in bindings:
            return bindings[node.id]
        return module.bindings.get(node.id, f"{module.name}.{node.id}")
    if isinstance(node, ast.Attribute):
        parent = _symbol(node.value, module, bindings)
        return f"{parent}.{node.attr}" if parent else None
    return None


@lru_cache(maxsize=1)
def _production_modules() -> dict[str, _Module]:
    modules: dict[str, _Module] = {}
    for path in sorted(_PACKAGE.rglob("*.py")):
        name, package = _module_name(path)
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        modules[name] = _Module(
            name=name,
            package=package,
            tree=tree,
            bindings=_module_bindings(name, package, tree),
        )
    return modules


def _load_modules(overrides: dict[str, str] | None = None) -> dict[str, _Module]:
    modules = dict(_production_modules())
    for name, source in (overrides or {}).items():
        original = modules[name]
        tree = ast.parse(source, filename=str(_ROOT.joinpath(*name.split(".")).with_suffix(".py")))
        modules[name] = _Module(
            name=name,
            package=original.package,
            tree=tree,
            bindings=_module_bindings(name, original.package, tree),
        )
    return modules


def _reexports(modules: dict[str, _Module]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for module in modules.values():
        for local, target in module.bindings.items():
            exported = f"{module.name}.{local}"
            if target != exported:
                aliases[exported] = target
    return aliases


def _canonical(symbol: str, aliases: dict[str, str]) -> str:
    seen: set[str] = set()
    while symbol in aliases and symbol not in seen:
        seen.add(symbol)
        symbol = aliases[symbol]
    return symbol


def _class_nodes(
    module: _Module,
) -> tuple[tuple[str, ast.ClassDef, dict[str, str | None]], ...]:
    found: list[tuple[str, ast.ClassDef, dict[str, str | None]]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.bindings: list[dict[str, str | None]] = [dict(module.bindings)]

        def _visible_bindings(self) -> dict[str, str | None]:
            visible: dict[str, str | None] = {}
            for scope in self.bindings:
                visible.update(scope)
            return visible

        def _resolve(self, node: ast.AST) -> str | None:
            return _symbol(node, module, self._visible_bindings())

        def _bind(self, name: str, target: str | None) -> None:
            self.bindings[-1][name] = target

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qualified = ".".join((*self.scope, node.name))
            found.append((qualified, node, self._visible_bindings()))
            self.scope.append(node.name)
            self.bindings.append({})
            for statement in node.body:
                self.visit(statement)
            self.bindings.pop()
            self.scope.pop()
            self._bind(node.name, f"{module.name}.{qualified}")

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope.append(f"{node.name}.<locals>")
            arguments = (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
            local_bindings = {argument.arg: None for argument in arguments}
            if node.args.vararg is not None:
                local_bindings[node.args.vararg.arg] = None
            if node.args.kwarg is not None:
                local_bindings[node.args.kwarg.arg] = None
            self.bindings.append(local_bindings)
            for statement in node.body:
                self.visit(statement)
            self.bindings.pop()
            self.scope.pop()
            qualified = ".".join((*self.scope, node.name))
            self._bind(node.name, f"{module.name}.{qualified}")

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node: ast.Assign) -> None:
            target = self._resolve(node.value)
            for assignment_target in node.targets:
                if isinstance(assignment_target, ast.Name):
                    self._bind(assignment_target.id, target)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name):
                target = self._resolve(node.value) if node.value is not None else None
                self._bind(node.target.id, target)

        def visit_Import(self, node: ast.Import) -> None:
            for imported in node.names:
                local = imported.asname or imported.name.split(".", 1)[0]
                self._bind(local, imported.name if imported.asname else local)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = _from_base(module.package, node)
            for imported in node.names:
                if imported.name == "*":
                    continue
                local = imported.asname or imported.name
                target = f"{base}.{imported.name}" if base else imported.name
                self._bind(local, target)

    Visitor().visit(module.tree)
    return tuple(found)


def _classes(modules: dict[str, _Module]) -> tuple[dict[str, _Class], dict[str, str]]:
    aliases = _reexports(modules)
    classes: dict[str, _Class] = {}
    for module in modules.values():
        for qualified_name, node, bindings in _class_nodes(module):
            fqn = f"{module.name}.{qualified_name}"
            bases = tuple(
                _canonical(symbol, aliases)
                for base in node.bases
                if (symbol := _symbol(base, module, bindings)) is not None
            )
            classes[fqn] = _Class(
                fqn=fqn,
                module=module,
                node=node,
                bases=bases,
                bindings=bindings,
            )
    return classes, aliases


def _structural_classes(classes: dict[str, _Class]) -> frozenset[str]:
    selected = set(_AUTHORITIES)
    changed = True
    while changed:
        changed = False
        for fqn, class_info in classes.items():
            if fqn in selected or not any(base in selected for base in class_info.bases):
                continue
            selected.add(fqn)
            changed = True
    return frozenset(selected)


def _mutable_model_classes(classes: dict[str, _Class]) -> frozenset[str]:
    selected = set(_MUTABLE_MODEL_BASES)
    changed = True
    while changed:
        changed = False
        for fqn, class_info in classes.items():
            if fqn in selected or not any(base in selected for base in class_info.bases):
                continue
            selected.add(fqn)
            changed = True
    return frozenset(selected)


def _class_authorities(class_info: _Class) -> frozenset[str]:
    authorities: set[str] = set()
    for node in class_info.node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            authorities.add(node.name)
        elif isinstance(node, ast.Assign):
            authorities.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            authorities.add(node.target.id)
    return frozenset(authorities)


def _mutable_model_violations(overrides: dict[str, str] | None = None) -> tuple[str, ...]:
    modules = _load_modules(overrides)
    classes, _aliases = _classes(modules)
    selected = _mutable_model_classes(classes)
    expected = frozenset(_MUTABLE_MODEL_INVENTORY)
    violations: list[str] = []
    if selected != expected:
        missing = sorted(expected - selected)
        unexpected = sorted(selected - expected)
        violations.append(f"mutable inventory drift: missing={missing}, unexpected={unexpected}")

    for fqn in sorted(selected):
        class_info = classes.get(fqn)
        if class_info is None:
            violations.append(f"missing mutable declaration: {fqn}")
            continue
        authorities = _class_authorities(class_info)
        if fqn not in _MUTABLE_MODEL_BASES and "__hash__" in authorities:
            violations.append(f"{fqn}: mutable descendant declares __hash__")
    return tuple(violations)


def _load_type(fqn: str) -> type:
    module_name, _, class_name = fqn.rpartition(".")
    return getattr(importlib.import_module(module_name), class_name)


def _load_object(fqn: str) -> object:
    module_name, _, attribute = fqn.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


def _dataclass_eq_false(class_info: _Class, aliases: dict[str, str]) -> bool:
    for decorator in class_info.node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        symbol = _symbol(decorator.func, class_info.module, class_info.bindings)
        if symbol is None or _canonical(symbol, aliases) != "dataclasses.dataclass":
            continue
        eq = next((kw.value for kw in decorator.keywords if kw.arg == "eq"), None)
        unsafe_hash = next(
            (kw.value for kw in decorator.keywords if kw.arg == "unsafe_hash"),
            None,
        )
        eq_is_false = isinstance(eq, ast.Constant) and eq.value is False
        hash_is_inherited = unsafe_hash is None or (
            isinstance(unsafe_hash, ast.Constant) and unsafe_hash.value is False
        )
        return eq_is_false and hash_is_inherited
    return False


def _violations(overrides: dict[str, str] | None = None) -> tuple[str, ...]:
    modules = _load_modules(overrides)
    classes, aliases = _classes(modules)
    selected = _structural_classes(classes)
    violations: list[str] = []
    if selected != _EXPECTED:
        missing = sorted(_EXPECTED - selected)
        unexpected = sorted(selected - _EXPECTED)
        violations.append(f"closed inventory drift: missing={missing}, unexpected={unexpected}")
    for fqn in sorted(selected):
        class_info = classes.get(fqn)
        if class_info is None:
            violations.append(f"missing class declaration: {fqn}")
            continue
        if not _dataclass_eq_false(class_info, aliases):
            violations.append(f"{fqn}: dataclass must declare eq=False")
        authorities = {
            node.name
            for node in class_info.node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in class_info.node.body:
            if isinstance(node, ast.Assign):
                authorities.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and isinstance(node.target, ast.Name)
            ):
                authorities.add(node.target.id)
        forbidden = sorted(authorities & {"__eq__", "__hash__"})
        if fqn not in _AUTHORITIES and forbidden:
            violations.append(f"{fqn}: custom equality authority {forbidden}")
    return tuple(violations)


def _source(module: str) -> str:
    path = _ROOT.joinpath(*module.split(".")).with_suffix(".py")
    return path.read_text()


def test_production_structural_identity_owners_are_closed():
    assert _violations() == ()


def test_production_mutable_model_owners_are_closed_and_unhashable():
    assert _mutable_model_violations() == ()
    assert all(not entry.key_consumers for entry in _MUTABLE_MODEL_INVENTORY.values())
    for entry in _MUTABLE_MODEL_INVENTORY.values():
        assert entry.mutation_evidence.strip()
        for consumer in (*entry.key_consumers, *entry.dedup_consumers):
            assert consumer.strip()
            assert callable(_load_object(consumer)), consumer
    assert {
        fqn: entry.dedup_consumers
        for fqn, entry in _MUTABLE_MODEL_INVENTORY.items()
        if entry.dedup_consumers
    } == {
        "lionagi.operations.fields.Instruct": (
            "lionagi.operations.fields._validate_listable_field",
        ),
        "lionagi.operations.fields.Reason": ("lionagi.operations.fields._validate_listable_field",),
        "lionagi.operations.fields.ActionRequestModel": (
            "lionagi.operations.fields._validate_listable_field",
        ),
        "lionagi.operations.fields.ActionResponseModel": (
            "lionagi.operations.fields._validate_listable_field",
        ),
    }
    for fqn in _MUTABLE_MODEL_INVENTORY:
        model_type = _load_type(fqn)
        assert model_type.__hash__ is None, fqn
        assert not issubclass(model_type, Hashable), fqn


def test_mutable_model_contract_rejects_an_unregistered_descendant():
    module = "lionagi.operations.fields"
    source = _source(module) + "\n\nclass RogueMutableModel(HashableModel):\n    pass\n"
    assert any(
        "RogueMutableModel" in item and "unexpected=" in item
        for item in _mutable_model_violations({module: source})
    )


def test_mutable_model_contract_rejects_a_descendant_hash_authority():
    module = "lionagi.operations.fields"
    source = _source(module).replace(
        "class Reason(HashableModel):\n",
        "class Reason(HashableModel):\n    __hash__ = object.__hash__\n",
        1,
    )
    assert any(
        "Reason: mutable descendant declares __hash__" in item
        for item in _mutable_model_violations({module: source})
    )


def test_mutable_model_contract_follows_a_function_local_base_alias():
    module = "lionagi.operations.fields"
    source = (
        _source(module)
        + "\n\ndef factory():\n"
        + "    Alias = HashableModel\n"
        + "    class HiddenMutable(Alias):\n"
        + "        __hash__ = object.__hash__\n"
        + "    return HiddenMutable\n"
    )
    violations = _mutable_model_violations({module: source})
    assert any("HiddenMutable" in item and "unexpected=" in item for item in violations)
    assert any("HiddenMutable: mutable descendant declares __hash__" in item for item in violations)


def test_contract_rejects_generated_dataclass_equality():
    module = "lionagi.operations.types"
    source = _source(module).replace(
        "@dataclass(slots=True, frozen=True, init=False, eq=False)\nclass MorphParam",
        "@dataclass(slots=True, frozen=True, init=False, eq=True)\nclass MorphParam",
        1,
    )
    assert any(
        "MorphParam: dataclass must declare eq=False" in item
        for item in _violations({module: source})
    )


def test_contract_rejects_generated_unsafe_hash():
    module = "lionagi.operations.types"
    source = _source(module).replace(
        "@dataclass(slots=True, frozen=True, init=False, eq=False)\nclass MorphParam",
        "@dataclass(slots=True, frozen=True, init=False, eq=False, unsafe_hash=True)\n"
        "class MorphParam",
        1,
    )
    assert any(
        "MorphParam: dataclass must declare eq=False" in item
        for item in _violations({module: source})
    )


def test_contract_rejects_undeclared_equality_authority():
    module = "lionagi.operations.types"
    source = _source(module).replace(
        'class MorphParam(Params):\n    """',
        'class MorphParam(Params):\n    def __eq__(self, other):\n        return True\n\n    """',
        1,
    )
    assert any(
        "MorphParam: custom equality authority ['__eq__']" in item
        for item in _violations({module: source})
    )


def test_contract_rejects_assigned_hash_authority():
    module = "lionagi.operations.types"
    source = _source(module).replace(
        'class MorphParam(Params):\n    """',
        'class MorphParam(Params):\n    __hash__ = object.__hash__\n\n    """',
        1,
    )
    assert any(
        "MorphParam: custom equality authority ['__hash__']" in item
        for item in _violations({module: source})
    )


def test_contract_rejects_an_unregistered_substrate_subclass():
    module = "lionagi.operations.types"
    source = _source(module) + "\n\nclass RogueParam(Params):\n    pass\n"
    violations = _violations({module: source})
    assert any("unexpected=['lionagi.operations.types.RogueParam']" in item for item in violations)
    assert any("RogueParam: dataclass must declare eq=False" in item for item in violations)


def test_contract_follows_a_module_level_base_alias():
    module = "lionagi.operations.types"
    source = (
        _source(module)
        + "\n\n_ParamsAlias = Params\n"
        + "@dataclass(slots=True, frozen=True, init=False)\n"
        + "class AliasRogueParam(_ParamsAlias):\n    pass\n"
    )
    violations = _violations({module: source})
    assert any(
        "unexpected=['lionagi.operations.types.AliasRogueParam']" in item for item in violations
    )
    assert any("AliasRogueParam: dataclass must declare eq=False" in item for item in violations)


def test_contract_follows_a_class_local_base_alias():
    module = "lionagi.operations.types"
    source = (
        _source(module)
        + "\n\nclass Namespace:\n"
        + "    Alias = Params\n"
        + "    @dataclass(slots=True, frozen=True, init=False)\n"
        + "    class HiddenParam(Alias):\n"
        + "        pass\n"
    )
    violations = _violations({module: source})
    assert any("HiddenParam" in item and "unexpected=" in item for item in violations)
    assert any("HiddenParam: dataclass must declare eq=False" in item for item in violations)


def test_contract_rejects_a_nested_substrate_subclass():
    module = "lionagi.operations.types"
    source = (
        _source(module)
        + "\n\ndef make_param():\n"
        + "    @dataclass(slots=True, frozen=True, init=False)\n"
        + "    class NestedRogueParam(Params):\n"
        + "        pass\n"
        + "    return NestedRogueParam\n"
    )
    violations = _violations({module: source})
    assert any("NestedRogueParam" in item and "unexpected=" in item for item in violations)
    assert any("NestedRogueParam: dataclass must declare eq=False" in item for item in violations)
