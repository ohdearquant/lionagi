"""ADR-0119 closed legacy sentinel-collapse compatibility contract."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lionagi.casts.pattern import Mode
from lionagi.ln import json_dumps
from lionagi.ln._async_call import BcallParams
from lionagi.ln.types import (
    DataClass,
    ModelConfig,
    Params,
    Spec,
    Undefined,
    Unset,
    is_sentinel,
    not_sentinel,
)
from lionagi.ln.types._sentinel import (
    LEGACY_SENTINEL_COLLAPSE_ALLOWLIST,
    _compat_is_sentinel,
)
from lionagi.models import Note
from lionagi.operations.types import RunParam
from lionagi.protocols.messages.action_response import ActionResponseContent

_EXPECTED_ALLOWLIST = frozenset(
    {
        ("lionagi.casts.pattern.Pattern._config", "none"),
        ("lionagi.casts.pattern.Pattern._config", "empty"),
        ("lionagi.casts.pattern.Role.emission_operable", "none"),
        ("lionagi.casts.pattern.Role.emission_operable", "empty"),
        ("lionagi.ln._async_call.AlcallParams._config", "none"),
        ("lionagi.models.field_model.FieldModel._config", "none"),
        ("lionagi.models.note._strip_sentinels", "none"),
        ("lionagi.models.note._strip_sentinels", "empty"),
        ("lionagi.operations.fields.Instruct.handle", "none"),
        ("lionagi.operations.fields.Instruct.handle", "empty"),
        ("lionagi.operations.types.MorphParam._config", "none"),
        ("lionagi.protocols.messages.instruction.InstructionContent._config", "none"),
        ("lionagi.protocols.messages.instruction.InstructionContent._config", "empty"),
        ("lionagi.protocols.messages.message.MessageContent._config", "none"),
    }
)

_PUBLIC_HELPERS = {
    f"{owner}.{name}"
    for owner in ("lionagi.ln", "lionagi.ln.types", "lionagi.ln.types._sentinel")
    for name in ("is_sentinel", "not_sentinel")
}
_PRIVATE_HELPERS = {
    f"lionagi.ln.types._sentinel.{name}"
    for name in ("_compat_policy", "_compat_is_sentinel", "_compat_not_sentinel")
}
_POLICY_TYPES = {"lionagi.ln.types._sentinel._SentinelPolicy"}
_MODEL_CONFIGS = {
    "lionagi.ln.ModelConfig",
    "lionagi.ln.types.ModelConfig",
    "lionagi.ln.types.base.ModelConfig",
}
_REPLACE = "dataclasses.replace"
_DIRECT_ONLY = _PUBLIC_HELPERS | _PRIVATE_HELPERS | _MODEL_CONFIGS
_DYNAMIC_DIRECT_SITE = "lionagi.models.note._strip_sentinels"
_SENTINEL_AUTHORITY_MODULE = "lionagi.ln.types._sentinel"
_BASE_MODULE = "lionagi.ln.types.base"
_PRIVATE_IMPORT_OWNERS = {
    "lionagi.ln.types._sentinel._compat_policy": {_BASE_MODULE},
    "lionagi.ln.types._sentinel._SentinelPolicy": {_BASE_MODULE},
    "lionagi.ln.types._sentinel._compat_is_sentinel": {
        "lionagi.casts.pattern",
        "lionagi.models.note",
    },
    "lionagi.ln.types._sentinel._compat_not_sentinel": {
        "lionagi.operations.fields",
    },
}
_PROTECTED_CARRIERS = {
    "lionagi.ln",
    "lionagi.ln.types",
    _SENTINEL_AUTHORITY_MODULE,
}.union(*_PRIVATE_IMPORT_OWNERS.values())


def _canonical_import_symbol(symbol: str) -> str:
    """Collapse LionAGI re-exports back to the protected physical authority."""
    if not symbol.startswith("lionagi."):
        return symbol
    leaf = symbol.rsplit(".", 1)[-1]
    if leaf in {"_compat_policy", "_compat_is_sentinel", "_compat_not_sentinel"}:
        return f"{_SENTINEL_AUTHORITY_MODULE}.{leaf}"
    if leaf == "_SentinelPolicy":
        return f"{_SENTINEL_AUTHORITY_MODULE}.{leaf}"
    if leaf in {"is_sentinel", "not_sentinel"}:
        return f"{_SENTINEL_AUTHORITY_MODULE}.{leaf}"
    if leaf == "ModelConfig":
        return "lionagi.ln.types.base.ModelConfig"
    return symbol


def _module_context(path: Path, root: Path) -> tuple[str, str]:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    module = ".".join(parts)
    package = module if is_package else module.rpartition(".")[0]
    return module, package


def _relative_import_base(package: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    parts = package.split(".") if package else []
    trim = node.level - 1
    if trim:
        parts = parts[:-trim]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _import_bindings(
    tree: ast.Module,
    module: str,
    package: str,
    *,
    label: str,
) -> tuple[dict[str, str], list[str], set[str]]:
    bindings: dict[str, str] = {}
    violations: list[str] = []
    protected_locals: set[str] = set()
    protected_symbols = _DIRECT_ONLY | _POLICY_TYPES | _PROTECTED_CARRIERS

    def bind(local: str, symbol: str, *, lineno: int) -> None:
        existing = bindings.get(local)
        protected = symbol in protected_symbols
        if existing is not None and existing != symbol and (local in protected_locals or protected):
            violations.append(f"{label}:{lineno}: import {local!r} shadows a protected binding")
            if local in protected_locals:
                return
        bindings[local] = symbol
        if protected:
            protected_locals.add(local)

    top_level = set(tree.body)
    for node in (item for item in ast.walk(tree) if isinstance(item, ast.Import | ast.ImportFrom)):
        is_top_level = node in top_level
        if isinstance(node, ast.ImportFrom):
            base = _relative_import_base(package, node)
            for imported in node.names:
                if imported.name == "*":
                    if base in _PROTECTED_CARRIERS:
                        violations.append(
                            f"{label}:{node.lineno}: protected star import from {base}"
                        )
                    continue
                local = imported.asname or imported.name
                symbol = _canonical_import_symbol(
                    ".".join(part for part in (base, imported.name) if part)
                )
                if not is_top_level:
                    if symbol in _DIRECT_ONLY | _POLICY_TYPES or local in protected_locals:
                        violations.append(
                            f"{label}:{node.lineno}: protected or shadowing import "
                            f"{symbol} is not module-level"
                        )
                    continue
                bind(local, symbol, lineno=node.lineno)
                allowed_owners = _PRIVATE_IMPORT_OWNERS.get(symbol)
                if allowed_owners is not None and module not in allowed_owners:
                    violations.append(
                        f"{label}:{node.lineno}: private symbol {symbol} cannot be imported by {module}"
                    )
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.asname:
                    local = imported.asname
                    symbol = imported.name
                else:
                    local = imported.name.split(".", 1)[0]
                    symbol = local
                if not is_top_level:
                    if imported.name == _SENTINEL_AUTHORITY_MODULE or local in protected_locals:
                        violations.append(
                            f"{label}:{node.lineno}: private or shadowing module import "
                            f"{imported.name} is not module-level"
                        )
                    continue
                bind(local, symbol, lineno=node.lineno)
                if (
                    imported.name == _SENTINEL_AUTHORITY_MODULE
                    and module != _SENTINEL_AUTHORITY_MODULE
                ):
                    violations.append(
                        f"{label}:{node.lineno}: private authority module cannot be imported as an object"
                    )
    return bindings, violations, protected_locals


def _symbol(node: ast.expr, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _symbol(node.value, bindings)
        return _canonical_import_symbol(f"{owner}.{node.attr}") if owner else None
    return None


def _bind_top_level_definitions(
    tree: ast.Module,
    module: str,
    bindings: dict[str, str],
) -> None:
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings[node.name] = f"{module}.{node.name}"


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _lexical_qualname(
    module: str,
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> str:
    names: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join((module, *reversed(names)))


def _class_qualname(
    module: str,
    node: ast.ClassDef,
    parents: dict[ast.AST, ast.AST],
) -> str:
    return f"{_lexical_qualname(module, node, parents)}.{node.name}"


def _literal_bool(node: ast.expr | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}


def _axis_value(call: ast.Call, axis: str, *, helper: str) -> ast.expr | None:
    keywords = _keywords(call)
    if axis in keywords:
        return keywords[axis]
    if helper == "not_sentinel":
        index = 1 if axis == "none_as_sentinel" else 2
        if len(call.args) > index:
            return call.args[index]
    return None


def _annotation_nodes(tree: ast.AST) -> set[ast.AST]:
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        elif isinstance(node, ast.arg) and node.annotation:
            roots.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            roots.append(node.returns)
    return {nested for root in roots for nested in ast.walk(root)}


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _target_names(item)}
    return set()


def _rebound_protected_imports(
    tree: ast.AST,
    protected_locals: set[str],
    *,
    label: str,
) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        rebound: set[str] = set()
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            rebound = {name for target in targets for name in _target_names(target)}
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            rebound = _target_names(node.target)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rebound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rebound.update(arg.arg for arg in node.args.posonlyargs)
                rebound.update(arg.arg for arg in node.args.args)
                rebound.update(arg.arg for arg in node.args.kwonlyargs)
                if node.args.vararg:
                    rebound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    rebound.add(node.args.kwarg.arg)
        for name in sorted(rebound & protected_locals):
            violations.append(
                f"{label}:{getattr(node, 'lineno', 0)}: protected import {name!r} is rebound"
            )
    return violations


def _protected_escape_violations(
    tree: ast.AST,
    module: str,
    bindings: dict[str, str],
    parents: dict[ast.AST, ast.AST],
    annotations: set[ast.AST],
    *,
    label: str,
) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        symbol = _symbol(node, bindings)
        if symbol in _PROTECTED_CARRIERS:
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue
            safe_getattr = (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Name)
                and parent.func.id == "getattr"
                and parent.args
                and parent.args[0] is node
                and len(parent.args) > 1
                and isinstance(parent.args[1], ast.Constant)
                and isinstance(parent.args[1].value, str)
                and f"{symbol}.{parent.args[1].value}" not in _DIRECT_ONLY | _POLICY_TYPES
                and f"{symbol}.{parent.args[1].value}" not in _PROTECTED_CARRIERS
            )
            if safe_getattr:
                continue
            violations.append(
                f"{label}:{node.lineno}: protected carrier {symbol} escapes member access"
            )
            continue
        if symbol in _POLICY_TYPES:
            if node in annotations:
                continue
            parent = parents.get(node)
            if (
                module == _SENTINEL_AUTHORITY_MODULE
                and isinstance(parent, ast.Call)
                and parent.func is node
                and _lexical_qualname(module, parent, parents)
                == f"{_SENTINEL_AUTHORITY_MODULE}._compat_policy"
            ):
                continue
            violations.append(
                f"{label}:{node.lineno}: private policy type {symbol} escapes annotation use"
            )
            continue
        if symbol not in _DIRECT_ONLY:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            continue
        if symbol in _MODEL_CONFIGS and node in annotations:
            continue
        violations.append(
            f"{label}:{node.lineno}: protected symbol {symbol} escapes direct invocation"
        )

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not (
            isinstance(call.func, ast.Name) and call.func.id in {"getattr", "vars"} and call.args
        ):
            continue
        carrier = _symbol(call.args[0], bindings)
        protected_getattr = False
        if carrier in _PROTECTED_CARRIERS and call.func.id == "getattr" and len(call.args) > 1:
            attribute = call.args[1]
            protected_getattr = not (
                isinstance(attribute, ast.Constant)
                and isinstance(attribute.value, str)
                and f"{carrier}.{attribute.value}" not in _DIRECT_ONLY | _POLICY_TYPES
                and f"{carrier}.{attribute.value}" not in _PROTECTED_CARRIERS
            )
        if carrier in _PROTECTED_CARRIERS and (call.func.id == "vars" or protected_getattr):
            violations.append(
                f"{label}:{call.lineno}: protected carrier {carrier} escapes through "
                f"{call.func.id}()"
            )
    for subscript in (node for node in ast.walk(tree) if isinstance(node, ast.Subscript)):
        carrier = _symbol(subscript.value, bindings)
        if carrier in _PROTECTED_CARRIERS:
            violations.append(
                f"{label}:{subscript.lineno}: protected carrier {carrier} is subscripted"
            )
    for attribute in (node for node in ast.walk(tree) if isinstance(node, ast.Attribute)):
        carrier = _symbol(attribute.value, bindings)
        if carrier in _PROTECTED_CARRIERS and attribute.attr == "__dict__":
            violations.append(
                f"{label}:{attribute.lineno}: protected carrier {carrier} exposes __dict__"
            )
    return violations


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _body_dump(source: str, function: str) -> list[str]:
    tree = ast.parse(source)
    node = next(
        item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function
    )
    return [ast.dump(item, include_attributes=False) for item in _without_docstring(node.body)]


_EXPECTED_CONFIG_POLICY_BODY = _body_dump(
    """
def _config_sentinel_policy(owner_key, _config_identity, config):
    owner = cast(type[Any], owner_key.target)
    return _compat_policy(
        site=f"{owner.__module__}.{owner.__qualname__}._config",
        none_as_sentinel=config.none_as_sentinel,
        empty_as_sentinel=config.empty_as_sentinel,
    )
""",
    "_config_sentinel_policy",
)
_EXPECTED_EFFECTIVE_POLICY_BODY = _body_dump(
    """
def _effective_config_sentinel_policy(model_type):
    config = model_type._config
    owner = next(
        base for base in model_type.__mro__ if base.__dict__.get("_config") is config
    )
    return _config_sentinel_policy(_IdentityKey(owner), id(config), config)
""",
    "_effective_config_sentinel_policy",
)
_EXPECTED_CONFIG_PREDICATE_BODY = _body_dump(
    """
def _is_config_sentinel(model_type, value):
    return _effective_config_sentinel_policy(model_type).is_sentinel(value)
""",
    "_is_config_sentinel",
)


def _validate_base_gateway(
    tree: ast.Module,
    bindings: dict[str, str],
    *,
    label: str,
) -> list[str]:
    violations: list[str] = []
    function_lists: dict[str, list[ast.FunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            function_lists.setdefault(node.name, []).append(node)
    expected = {
        "_config_sentinel_policy": (
            ("owner_key", "_config_identity", "config"),
            _EXPECTED_CONFIG_POLICY_BODY,
        ),
        "_effective_config_sentinel_policy": (
            ("model_type",),
            _EXPECTED_EFFECTIVE_POLICY_BODY,
        ),
        "_is_config_sentinel": (
            ("model_type", "value"),
            _EXPECTED_CONFIG_PREDICATE_BODY,
        ),
    }
    for name, (arguments, body) in expected.items():
        candidates = function_lists.get(name, [])
        if len(candidates) != 1:
            violations.append(
                f"{label}: expected exactly one closed gateway function {name}, "
                f"found {len(candidates)}"
            )
            continue
        function = candidates[0]
        actual_arguments = tuple(arg.arg for arg in function.args.args)
        has_extra_signature_parts = bool(
            function.args.posonlyargs
            or function.args.kwonlyargs
            or function.args.vararg
            or function.args.kwarg
            or function.args.defaults
            or any(default is not None for default in function.args.kw_defaults)
        )
        if actual_arguments != arguments or has_extra_signature_parts:
            violations.append(f"{label}:{function.lineno}: {name} signature drift")
        actual_body = [
            ast.dump(item, include_attributes=False) for item in _without_docstring(function.body)
        ]
        if actual_body != body:
            violations.append(f"{label}:{function.lineno}: {name} body drift")
        if name != "_config_sentinel_policy" and function.decorator_list:
            violations.append(f"{label}:{function.lineno}: {name} must not be decorated")

    reserved = set(expected)
    gateway_parents = _parents(tree)

    def module_runtime(node: ast.AST) -> bool:
        current = gateway_parents.get(node)
        while current is not None:
            if isinstance(
                current,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
            ):
                return False
            current = gateway_parents.get(current)
        return True

    expected_nodes = {
        candidates[0] for name in reserved if len(candidates := function_lists.get(name, [])) == 1
    }
    for node in ast.walk(tree):
        if not module_runtime(node):
            continue
        rebound: set[str] = set()
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            rebound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node not in expected_nodes:
                rebound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for imported in node.names:
                if isinstance(node, ast.Import):
                    rebound.add(imported.asname or imported.name.split(".", 1)[0])
                else:
                    rebound.add(imported.asname or imported.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            rebound.add(node.name)
        elif isinstance(node, ast.MatchAs) and node.name:
            rebound.add(node.name)
        for name in sorted(rebound & reserved):
            violations.append(f"{label}:{node.lineno}: closed gateway {name} is rebound")

    policy_candidates = function_lists.get("_config_sentinel_policy", [])
    policy = policy_candidates[0] if len(policy_candidates) == 1 else None
    if policy is not None:
        if len(policy.decorator_list) != 1:
            violations.append(f"{label}:{policy.lineno}: policy cache decorator drift")
        else:
            decorator = policy.decorator_list[0]
            valid_cache = (
                isinstance(decorator, ast.Call)
                and _symbol(decorator.func, bindings) == "functools.lru_cache"
                and not decorator.args
                and len(decorator.keywords) == 1
                and decorator.keywords[0].arg == "maxsize"
                and isinstance(decorator.keywords[0].value, ast.Constant)
                and isinstance(decorator.keywords[0].value.value, int)
                and decorator.keywords[0].value.value > 0
            )
            if not valid_cache:
                violations.append(f"{label}:{policy.lineno}: policy cache is not bounded")
    return violations


def _model_config_axes(
    call: ast.Call,
    *,
    label: str,
) -> tuple[dict[str, bool], list[str]]:
    violations: list[str] = []
    axes: dict[str, bool] = {}
    if call.args:
        violations.append(f"{label}:{call.lineno}: ModelConfig uses positional arguments")
    if any(keyword.arg is None for keyword in call.keywords):
        violations.append(f"{label}:{call.lineno}: ModelConfig uses **kwargs")
    for keyword, axis in (
        ("none_as_sentinel", "none"),
        ("empty_as_sentinel", "empty"),
    ):
        raw = _keywords(call).get(keyword)
        if raw is None:
            axes[axis] = False
        elif _literal_bool(raw) is None:
            violations.append(f"{label}:{call.lineno}: dynamic ModelConfig {keyword}")
        else:
            axes[axis] = bool(_literal_bool(raw))
    return axes, violations


def _contains_model_config(value: ast.AST | None, bindings: dict[str, str]) -> bool:
    return value is not None and any(
        isinstance(node, ast.Call) and _symbol(node.func, bindings) in _MODEL_CONFIGS
        for node in ast.walk(value)
    )


def _contains_collapse_axis_syntax(value: ast.AST | None) -> bool:
    if value is None:
        return False
    axes = {"none_as_sentinel", "empty_as_sentinel"}
    for node in ast.walk(value):
        if isinstance(node, ast.keyword) and node.arg in axes:
            return True
        if isinstance(node, ast.Constant) and node.value in axes:
            return True
    return False


def _runtime_substrate_mutation_violations(
    tree: ast.Module,
    *,
    label: str,
) -> list[str]:
    """Reject supported-code mutations that could forge a declared config owner."""
    violations: list[str] = []
    protected_attributes = {"_config", "__module__", "__qualname__"}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            for nested in ast.walk(target):
                if not isinstance(nested, ast.Attribute):
                    continue
                if nested.attr not in protected_attributes:
                    continue
                if (
                    nested.attr == "_config"
                    and isinstance(nested.value, ast.Name)
                    and nested.value.id == "self"
                ):
                    continue
                if (
                    nested.attr == "_config"
                    and isinstance(nested.value, ast.Name)
                    and nested.value.id != "cls"
                    and not nested.value.id[:1].isupper()
                ):
                    continue
                violations.append(
                    f"{label}:{node.lineno}: runtime mutation of {nested.attr} is unsupported"
                )
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in protected_attributes
        ):
            continue
        attribute = node.args[1].value
        if isinstance(node.args[0], ast.Name):
            target = node.args[0].id
            if attribute == "_config" and target == "self":
                continue
            if attribute == "_config" and target != "cls" and not target[:1].isupper():
                continue
        violations.append(
            f"{label}:{node.lineno}: reflective mutation of {node.args[1].value} is unsupported"
        )
    return violations


def _inspect_module(
    source: str,
    *,
    module: str,
    package: str | None = None,
    label: str = "<source>",
) -> tuple[frozenset[tuple[str, str]], tuple[str, ...]]:
    package = package if package is not None else module.rpartition(".")[0]
    tree = ast.parse(source, filename=label)
    parents = _parents(tree)
    bindings, violations, protected_locals = _import_bindings(
        tree,
        module,
        package,
        label=label,
    )
    _bind_top_level_definitions(tree, module, bindings)
    violations.extend(_rebound_protected_imports(tree, protected_locals, label=label))
    violations.extend(
        _protected_escape_violations(
            tree,
            module,
            bindings,
            parents,
            _annotation_nodes(tree),
            label=label,
        )
    )
    if module == _BASE_MODULE:
        violations.extend(_validate_base_gateway(tree, bindings, label=label))
    violations.extend(_runtime_substrate_mutation_violations(tree, label=label))

    observed: set[tuple[str, str]] = set()
    config_calls: set[ast.Call] = set()
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for statement in class_node.body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets, value = [statement.target], statement.value
            target_names = {name for target in targets for name in _target_names(target)}
            if "_config" not in target_names:
                continue
            is_direct = (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and targets[0].id == "_config"
                and isinstance(value, ast.Call)
                and _symbol(value.func, bindings) in _MODEL_CONFIGS
            )
            is_substrate_config = _contains_model_config(
                value, bindings
            ) or _contains_collapse_axis_syntax(value)
            if not is_direct and is_substrate_config:
                violations.append(
                    f"{label}:{statement.lineno}: class _config must be direct ModelConfig(...)"
                )
                continue
            if not is_direct:
                continue
            assert isinstance(value, ast.Call)
            config_calls.add(value)
            axes, axis_violations = _model_config_axes(value, label=label)
            violations.extend(axis_violations)
            site = f"{_class_qualname(module, class_node, parents)}._config"
            observed.update((site, axis) for axis, enabled in axes.items() if enabled)

    private_policy_calls = 0
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        symbol = _symbol(call.func, bindings)
        if symbol in _MODEL_CONFIGS:
            if call not in config_calls:
                axes, axis_violations = _model_config_axes(call, label=label)
                violations.extend(axis_violations)
                if any(axes.values()):
                    violations.append(
                        f"{label}:{call.lineno}: collapse-enabled ModelConfig outside class _config"
                    )
            continue

        if symbol == _REPLACE:
            keywords = _keywords(call)
            if any(axis in keywords for axis in ("none_as_sentinel", "empty_as_sentinel")):
                violations.append(
                    f"{label}:{call.lineno}: dataclasses.replace mutates collapse axes"
                )
            if (
                any(keyword.arg is None for keyword in call.keywords)
                and call.args
                and _contains_model_config(call.args[0], bindings)
            ):
                violations.append(
                    f"{label}:{call.lineno}: ModelConfig dataclasses.replace uses **kwargs"
                )
            continue

        if symbol in _PUBLIC_HELPERS:
            helper = symbol.rsplit(".", 1)[-1]
            if len(call.args) != 1 or any(
                isinstance(argument, ast.Starred) for argument in call.args
            ):
                violations.append(
                    f"{label}:{call.lineno}: public {helper} must receive one positional value"
                )
            if any(keyword.arg is None for keyword in call.keywords):
                violations.append(f"{label}:{call.lineno}: public {helper} uses **kwargs")
            for keyword in ("none_as_sentinel", "empty_as_sentinel"):
                raw = _axis_value(call, keyword, helper=helper)
                if raw is not None and _literal_bool(raw) is not False:
                    violations.append(
                        f"{label}:{call.lineno}: public {helper} requests legacy collapse"
                    )
            continue

        if symbol not in _PRIVATE_HELPERS:
            continue

        caller = _lexical_qualname(module, call, parents)
        helper = symbol.rsplit(".", 1)[-1]
        if module == _SENTINEL_AUTHORITY_MODULE:
            allowed_edge = (caller, helper) in {
                (f"{_SENTINEL_AUTHORITY_MODULE}._compat_is_sentinel", "_compat_policy"),
                (
                    f"{_SENTINEL_AUTHORITY_MODULE}._compat_not_sentinel",
                    "_compat_is_sentinel",
                ),
            }
            if not allowed_edge:
                violations.append(
                    f"{label}:{call.lineno}: unrecognized private authority edge "
                    f"{caller} -> {helper}"
                )
            continue

        if helper == "_compat_policy":
            private_policy_calls += 1
            if caller != f"{_BASE_MODULE}._config_sentinel_policy":
                violations.append(
                    f"{label}:{call.lineno}: _compat_policy outside the compiled config gateway"
                )
            continue

        if call.args and any(isinstance(argument, ast.Starred) for argument in call.args):
            violations.append(f"{label}:{call.lineno}: private gateway uses *args")
        if len(call.args) > 1 or any(keyword.arg is None for keyword in call.keywords):
            violations.append(f"{label}:{call.lineno}: private gateway argument shape drift")
        keywords = _keywords(call)
        site_node = keywords.get("site")
        if not isinstance(site_node, ast.Constant) or not isinstance(site_node.value, str):
            violations.append(f"{label}:{call.lineno}: private gateway site is not literal")
            continue
        site = site_node.value
        if site != caller:
            violations.append(f"{label}:{call.lineno}: private gateway site {site!r} != {caller!r}")
        for keyword, axis in (
            ("none_as_sentinel", "none"),
            ("empty_as_sentinel", "empty"),
        ):
            raw = keywords.get(keyword)
            enabled = _literal_bool(raw)
            if site == _DYNAMIC_DIRECT_SITE and isinstance(raw, ast.Name):
                if raw.id != keyword:
                    violations.append(
                        f"{label}:{call.lineno}: Note dynamic axis {keyword} uses {raw.id}"
                    )
                else:
                    observed.add((site, axis))
            elif raw is not None and enabled is None:
                violations.append(f"{label}:{call.lineno}: dynamic private {keyword}")
            elif enabled:
                observed.add((site, axis))

    if module == _BASE_MODULE and private_policy_calls != 1:
        violations.append(
            f"{label}: expected exactly one compiled config compatibility gateway, "
            f"found {private_policy_calls}"
        )
    return frozenset(observed), tuple(violations)


def _production_inventory() -> tuple[frozenset[tuple[str, str]], tuple[str, ...]]:
    root = Path(__file__).resolve().parents[2] / "lionagi"
    observed: set[tuple[str, str]] = set()
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        module, package = _module_context(path, root)
        module_observed, module_violations = _inspect_module(
            path.read_text(encoding="utf-8"),
            module=module,
            package=package,
            label=str(path),
        )
        observed.update(module_observed)
        violations.extend(module_violations)
    return frozenset(observed), tuple(violations)


def test_public_helpers_reject_unnamed_legacy_collapse():
    with pytest.raises(ValueError, match="allowlisted"):
        is_sentinel(None, none_as_sentinel=True)
    with pytest.raises(ValueError, match="allowlisted"):
        not_sentinel(None, True)
    with pytest.raises(ValueError, match="allowlisted"):
        is_sentinel([], empty_as_sentinel=True)


def test_public_helpers_keep_identity_only_default_semantics():
    assert is_sentinel(Undefined)
    assert is_sentinel(Unset)
    assert not is_sentinel(None)
    assert not is_sentinel(False)
    assert not is_sentinel(0)
    assert not is_sentinel("")
    assert not_sentinel(None)


def test_every_allowlisted_axis_is_enforced_by_the_runtime_gateway():
    by_site: dict[str, set[str]] = {}
    for site, axis in LEGACY_SENTINEL_COLLAPSE_ALLOWLIST:
        by_site.setdefault(site, set()).add(axis)

    for site, axes in by_site.items():
        assert _compat_is_sentinel(
            None,
            site=site,
            none_as_sentinel="none" in axes,
            empty_as_sentinel=False,
        ) is ("none" in axes)
        assert _compat_is_sentinel(
            "",
            site=site,
            none_as_sentinel=False,
            empty_as_sentinel="empty" in axes,
        ) is ("empty" in axes)


def test_legacy_collapse_authority_matches_the_closed_production_inventory():
    observed, violations = _production_inventory()

    assert LEGACY_SENTINEL_COLLAPSE_ALLOWLIST == _EXPECTED_ALLOWLIST
    assert violations == ()
    assert observed == LEGACY_SENTINEL_COLLAPSE_ALLOWLIST


@pytest.mark.parametrize(
    "source",
    [
        """
from lionagi.ln.types._sentinel import _compat_is_sentinel
alias = _compat_is_sentinel
""",
        """
from lionagi.ln.types._sentinel import _compat_is_sentinel
def check(value, callback=_compat_is_sentinel):
    return callback(value)
""",
        """
from functools import partial
from lionagi.ln.types._sentinel import _compat_is_sentinel
check = partial(_compat_is_sentinel, site='lionagi.models.note._strip_sentinels')
""",
        """
from functools import partial
from lionagi.ln.types import ModelConfig
factory = partial(ModelConfig, none_as_sentinel=True)
""",
        """
import lionagi.ln.types._sentinel as sentinel_module
check = getattr(sentinel_module, '_compat_is_sentinel')
""",
        """
import lionagi.ln.types._sentinel as sentinel_module
registry = sentinel_module.__dict__
""",
        """
import lionagi.ln.types._sentinel as sentinel_module
alias = sentinel_module
""",
        """
from lionagi.ln.types._sentinel import _SentinelPolicy
policy = _SentinelPolicy(True, False)
""",
        """
from lionagi.ln.types._sentinel import _compat_is_sentinel as compat
__all__ = ('compat',)
""",
        """
from dataclasses import replace
from lionagi.ln.types import ModelConfig
class Bad:
    _config = replace(ModelConfig(), **{'none_as_sentinel': True})
""",
        """
from lionagi.ln.types import is_sentinel as check
def bad(value):
    return check(value, none_as_sentinel=True)
""",
        """
from lionagi.ln.types._sentinel import _compat_is_sentinel
def local(value):
    return _compat_is_sentinel(
        value,
        site='lionagi.models.note._strip_sentinels',
        none_as_sentinel=True,
    )
""",
    ],
)
def test_static_contract_rejects_alias_factory_and_spoof_bypasses(source: str):
    _, violations = _inspect_module(source, module="lionagi.synthetic")
    assert violations


def test_static_contract_resolves_relative_aliases_without_leaf_name_false_positives():
    source = """
import dataclasses
import lionagi.ln as ln
from .ln.types import is_sentinel as check

class DomainDetector:
    def is_sentinel(self, value, *, none_as_sentinel=False):
        return False

def replace(value, *, none_as_sentinel=False):
    return value

class Cache:
    _config = {'unrelated': True}

def valid(value):
    check(value)
    DomainDetector().is_sentinel(value, none_as_sentinel=True)
    replace(value, none_as_sentinel=True)
    getattr(ln, 'json_dumps')
    dataclasses.replace(value, **{'unrelated': True})
"""
    observed, violations = _inspect_module(source, module="lionagi.synthetic")
    assert observed == frozenset()
    assert violations == ()


def test_static_contract_rejects_gateway_shape_mutation():
    path = Path(__file__).resolve().parents[2] / "lionagi/ln/types/base.py"
    source = path.read_text(encoding="utf-8")
    mutations = (
        source.replace(
            'site=f"{owner.__module__}.{owner.__qualname__}._config"',
            'site="lionagi.operations.types.MorphParam._config"',
            1,
        ),
        source.replace(
            "none_as_sentinel=config.none_as_sentinel",
            "none_as_sentinel=True",
            1,
        ),
        source.replace(
            'base.__dict__.get("_config") is config',
            "base is model_type",
            1,
        ),
        source + "\n_config_sentinel_policy = lambda *args: None\n",
        source + "\nclass _config_sentinel_policy:\n    pass\n",
        source + "\nasync def _config_sentinel_policy():\n    pass\n",
        source + "\nfrom external import _config_sentinel_policy\n",
        source.replace(
            "def _effective_config_sentinel_policy(model_type: type[Any])",
            "@staticmethod\ndef _effective_config_sentinel_policy(model_type: type[Any])",
            1,
        ),
        source.replace(
            "from functools import lru_cache",
            "def lru_cache(*args, **kwargs):\n    return lambda function: function",
            1,
        ),
    )
    for mutated in mutations:
        _, violations = _inspect_module(mutated, module=_BASE_MODULE)
        assert violations


@pytest.mark.parametrize(
    "target",
    (
        "_config = other = ModelConfig(none_as_sentinel=True)",
        "_config, other = ModelConfig(none_as_sentinel=True), None",
    ),
)
def test_static_contract_rejects_non_direct_substrate_config_targets(target: str):
    source = f"""
from lionagi.ln.types import ModelConfig
class Invalid:
    {target}
"""
    _, violations = _inspect_module(source, module="lionagi.synthetic")
    assert any("must be direct ModelConfig" in violation for violation in violations)


def test_static_contract_rejects_nested_shadowing_of_a_protected_import():
    source = """
from lionagi.ln.types import is_sentinel as check
def wrapper(value):
    from unrelated import check
    return check(value)
"""
    _, violations = _inspect_module(source, module="lionagi.synthetic")
    assert any("shadowing import" in violation for violation in violations)


@pytest.mark.parametrize(
    "imports",
    (
        """
from lionagi.ln.types._sentinel import _compat_is_sentinel as check
from unrelated import check
""",
        """
from unrelated import check
from lionagi.ln.types._sentinel import _compat_is_sentinel as check
""",
    ),
)
def test_static_contract_rejects_ordered_import_shadowing(imports: str):
    source = f"""
{imports}
check(None, site='lionagi.casts.pattern', none_as_sentinel=True)
"""
    _, violations = _inspect_module(source, module="lionagi.casts.pattern")
    assert any("shadows a protected binding" in violation for violation in violations)


@pytest.mark.parametrize(
    "source",
    (
        "from lionagi.models.note import _compat_is_sentinel",
        "from lionagi.ln.types.base import _compat_policy",
        "from lionagi.ln.types.base import _SentinelPolicy",
        """
import lionagi.models.note as note
def local(value):
    return note._compat_is_sentinel(
        value,
        site='lionagi.models.note._strip_sentinels',
        none_as_sentinel=True,
    )
""",
    ),
)
def test_static_contract_rejects_private_reexports(source: str):
    _, violations = _inspect_module(source, module="lionagi.synthetic")
    assert violations


def test_static_contract_rejects_runtime_config_owner_forgery():
    source = """
from dataclasses import replace
from lionagi.ln.types import Params

class Rogue(Params):
    pass

flags = {'none_as_sentinel': True}
Rogue._config = replace(Rogue._config, **flags)
Rogue.__module__ = 'lionagi.operations.types'
Rogue.__qualname__ = 'MorphParam'
"""
    _, violations = _inspect_module(source, module="lionagi.synthetic")
    assert sum("unsupported" in violation for violation in violations) == 3


def test_static_contract_rejects_cls_runtime_owner_forgery():
    source = """
from dataclasses import replace
from lionagi.ln.types import Params

class Rogue(Params):
    @classmethod
    def forge(cls):
        cls._config = replace(
            cls._config,
            **{'none_as_sentinel': True},
        )
        setattr(cls, '__module__', 'lionagi.operations.types')
        setattr(cls, '__qualname__', 'MorphParam')
"""
    _, violations = _inspect_module(source, module="lionagi.synthetic")
    assert sum("unsupported" in violation for violation in violations) == 3


def test_allowlisted_site_cannot_claim_an_unlisted_axis():
    none_only_site = next(
        site
        for site, axis in LEGACY_SENTINEL_COLLAPSE_ALLOWLIST
        if axis == "none" and (site, "empty") not in LEGACY_SENTINEL_COLLAPSE_ALLOWLIST
    )

    with pytest.raises(ValueError, match="not allowlisted"):
        _compat_is_sentinel("", site=none_only_site, empty_as_sentinel=True)


def test_legacy_empty_collapse_never_absorbs_false_or_zero():
    both_site = next(
        site
        for site, axis in LEGACY_SENTINEL_COLLAPSE_ALLOWLIST
        if axis == "empty" and (site, "none") in LEGACY_SENTINEL_COLLAPSE_ALLOWLIST
    )

    for value in ("", (), set(), frozenset(), {}, []):
        assert _compat_is_sentinel(value, site=both_site, empty_as_sentinel=True)
    for value in (False, 0):
        assert not _compat_is_sentinel(value, site=both_site, empty_as_sentinel=True)


def test_inherited_configs_resolve_the_declaring_allowlisted_owner():
    assert Mode._is_sentinel(None)
    assert Mode._is_sentinel("")
    assert BcallParams._is_sentinel(None)
    assert not BcallParams._is_sentinel("")
    assert RunParam._is_sentinel(None)
    assert not RunParam._is_sentinel("")
    assert ActionResponseContent._is_sentinel(None)


def test_compiled_policy_revalidates_when_the_effective_config_object_changes():
    class RuntimeReconfiguredParams(Params):
        pass

    assert not RuntimeReconfiguredParams._is_sentinel(None)
    RuntimeReconfiguredParams._config = ModelConfig(none_as_sentinel=True)

    with pytest.raises(ValueError, match="not allowlisted"):
        RuntimeReconfiguredParams._is_sentinel(None)


def test_compiled_policy_revalidates_when_same_object_gets_a_nearer_owner():
    class RuntimeReownedParams(RunParam):
        pass

    assert RuntimeReownedParams._is_sentinel(None)
    RuntimeReownedParams._config = RunParam._config

    with pytest.raises(ValueError, match="not allowlisted"):
        RuntimeReownedParams._is_sentinel(None)


def test_batch_paths_preserve_the_custom_is_sentinel_override_seam():
    @dataclass(frozen=True, slots=True, init=False)
    class CustomOmissionParams(Params):
        value: str

        @classmethod
        def _is_sentinel(cls, value: Any) -> bool:
            return value == "custom-missing" or Params._is_sentinel(value)

    @dataclass(frozen=True, slots=True, init=False)
    class StrictCustomParams(CustomOmissionParams):
        _config = ModelConfig(strict=True)

    assert CustomOmissionParams(value="custom-missing").to_dict() == {}
    with pytest.raises(ValueError, match="Missing required parameter: value"):
        StrictCustomParams(value="custom-missing")

    @dataclass(frozen=True, slots=True, init=False)
    class InstanceOverrideParams(Params):
        value: str

        def _is_sentinel(self, value: Any) -> bool:
            return value == "custom-missing" or Params._is_sentinel(value)

    assert InstanceOverrideParams(value="custom-missing").to_dict() == {}

    @dataclass(slots=True)
    class CustomDataClass(DataClass):
        value: str = "present"
        derived: str = field(default="present", init=False)
        _config = ModelConfig(strict=True)

        @classmethod
        def _is_sentinel(cls, value: Any) -> bool:
            return value == "custom-missing" or DataClass._is_sentinel(value)

    current = CustomDataClass()
    current.derived = "custom-missing"
    assert current.to_dict() == {"value": "present"}
    with pytest.raises(ValueError, match="Missing required parameter: derived"):
        current.with_updates(value="changed")


def test_note_keeps_legacy_recursive_omission_without_collapsing_false_or_zero():
    note = Note(
        content={
            "none": None,
            "empty": "",
            "false": False,
            "zero": 0,
            "nested": [None, "", False, 0, {"none": None, "keep": "value"}],
        }
    )

    assert note.to_dict(exclude_none=True, exclude_empty=True) == {
        "false": False,
        "zero": 0,
        "nested": [False, 0, {"keep": "value"}],
    }


def test_spec_omits_unresolved_base_type_through_the_explicit_json_projection():
    spec = Spec()

    assert spec.base_type is Undefined
    assert spec.annotation is Any
    assert spec.to_dict(mode="json") == {"metadata": []}
    with pytest.raises(TypeError, match="UndefinedType"):
        json_dumps(spec, as_loaded=True)
