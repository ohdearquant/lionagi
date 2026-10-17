"""Closed mutable-model declarations and statically resolvable hash authority.

The scan follows imports, reexports, lexical name/attribute aliases (assignments,
assignment expressions, and loop targets over a literal tuple, list or set) and
declared inheritance. A name bound in more than one place resolves to every value it
was given, so a fallback in an ``except`` clause cannot hide the import it replaces.
Calls are read in every expression a statement evaluates, including tests, context
managers, loop iterables, return values, assertions and match subjects and guards. Every
statement body is walked, a ``match`` statement's case clauses included, and a statement
kind the walk cannot classify is reported rather than skipped.

The scan fails closed by construction: it reads only the shapes it fully understands, and
refuses every other one. The shapes it reads are a class base that is a plain name or a
dotted attribute; class keywords, decorator keywords and ``model_config`` entries written
as named keyword arguments; a ``model_config`` value given as a dict literal with string
keys, or as a call whose own arguments are named keyword arguments with readable values;
and a ``model_config`` assigned to a single plain name. A decorator is read when its name
is imported into the module and it is called without positional arguments. Everything else
is a "cannot read" violation, naming the file and line: a statement kind the walk does not
know, a class base, keyword, decorator or ``model_config`` assignment outside those forms,
an attribute assignment whose owner is not a plain name, an assignment to ``__bases__``,
``__class__`` or ``__mro__``, a ``del`` of an attribute whose owner is not a plain name, a
class built by a ``type(...)`` call, and a ``setattr`` whose owner or attribute is not a
literal the scan reads. Each is refused unless it is listed in the test's allowlist with a
reason. A class a function builds from one of its parameters, or one ``create_model``
builds, is outside what the scan proves, as the inventory's scope limits say. Consumer
references
document audited boundaries rather than arbitrary dataflow. Wildcard imports are
rejected because they can conceal declared descendants. Runtime container and mutation
contracts cover the inventoried concrete types.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_INVENTORY = Path(__file__).with_name("data") / "mutable_model_inventory.json"
_ROOTS = frozenset(
    {"lionagi.ln.types.base.DataClass", "lionagi.models.hashable_model.HashableModel"}
)
# A loop or with target the scan cannot resolve to a declaration.
_UNRESOLVED = "<unresolved>"
_SETATTR = frozenset(
    {"builtins.setattr", "builtins.type.__setattr__", "builtins.object.__setattr__"}
)
# Assigning one of these rebinds what a class is, so a class the scan inventories
# could take a new base or a new metaclass after its declaration.
_AUTHORITY_ATTRIBUTES = frozenset({"__bases__", "__class__", "__mro__"})
# The attribute names a call writes an attribute through; a call through one whose
# function the scan cannot resolve is refused rather than skipped.
_DELATTR_ATTRIBUTES = frozenset({"__setattr__", "__delattr__", "__delitem__"})
# The keyword names that turn on a generated hash or a frozen configuration. The
# scan decides these by their value; every other named keyword is read as not one.
_HASH_KEYWORDS = frozenset({"unsafe_hash", "frozen"})


# A shape the scan cannot read is refused by default; the entries here are the
# production shapes audited and accepted, each with the reason it is safe. The key is
# ``module:line:column`` of the refused node, as the scan reports it. Every other
# refusal fails the gate, so a new shape cannot pass unnoticed.
_ALLOWLIST: dict[str, str] = {
    # ``setattr`` writes an attribute through a name the scan does not read as a
    # literal. Each is a dynamic attribute name on an already-mutable object or an
    # object's own field slot, none of them a class's ``__hash__`` or a class
    # authority:
    "lionagi._auto:316:9": "a marker written onto a handler function, not a model",
    "lionagi.ln.types.base:224:13": "a declared field set from its layout during init",
    "lionagi.ln.types.base:367:13": "a declared field copied by name during update",
    "lionagi.models.operable_model:132:13": "an extra field set by name",
    "lionagi.models.operable_model:142:17": "an extra field reset to its default",
    "lionagi.models.operable_model:145:17": "an extra field reset from its factory",
    "lionagi.models.operable_model:255:9": "an extra field set by name",
    "lionagi.models.operable_model:275:13": "a field attribute set by name on a field object",
    "lionagi.protocols.generic.pile:527:13": "a field attribute copied by name while copying",
    "lionagi.protocols.messages.instruction:100:17": "a declared field set by name",
    "lionagi.protocols.messages.instruction:146:17": "a declared field set by name",
    "lionagi.protocols.messages.message:181:9": "a declared field set by name",
    "lionagi.protocols.messages.message:193:13": "a render-input field set by name",
    "lionagi.providers.anthropic.claude_code:1100:17": "a runtime field copied by name",
    "lionagi.service.connections.endpoint_config:201:17": "a config key set by name",
    "lionagi.service.connections.mcp_wrapper:76:9": "a probe attribute set on an exception",
    # A class base the scan cannot read: a parameterized generic base, which declares
    # no hash authority of its own and is outside the mutable family.
    "lionagi.adapters._base:331:15": "a runtime-checkable protocol parameterized by a type",
    "lionagi.adapters._base:508:20": "a runtime-checkable protocol parameterized by a type",
    "lionagi.adapters.async_postgres_adapter:21:39": "an adapter parameterized by a type",
    "lionagi.adapters.csv_:26:31": "an adapter parameterized by a type",
    "lionagi.adapters.json_:26:32": "an adapter parameterized by a type",
    "lionagi.adapters.pandas_:37:37": "an adapter parameterized by a type",
    "lionagi.adapters.toml_:33:32": "an adapter parameterized by a type",
    "lionagi.ln._cache:19:23": "a cache parameterized by two types",
    "lionagi.ln.concurrency.primitives:126:13": "a primitive parameterized by a type",
    "lionagi.protocols._concepts:110:21": "a concept parameterized by a type",
    "lionagi.protocols._concepts:98:23": "a concept parameterized by a type",
    "lionagi.protocols.generic.element:274:10": "an element parameterized by a type",
    "lionagi.protocols.generic.flow:25:21": "a flow parameterized by two types",
    "lionagi.protocols.generic.pile:175:21": "a pile parameterized by two types",
    "lionagi.protocols.generic.pile:175:36": "a pile parameterized by two types",
    "lionagi.protocols.generic.progression:139:28": "a progression parameterized by a type",
    "lionagi.protocols.generic.progression:139:41": "a progression parameterized by a type",
    "lionagi.protocols.graph.graph:33:34": "a graph parameterized by a type",
    # A metaclass the scan does not inventory; the class declares no hash authority
    # and is outside the mutable family:
    "lionagi.ln.types._sentinel:38:1": "a singleton metaclass, not a mutable model",
    # An attribute assignment whose owner is an item of a list, not a name or a
    # dotted attribute; the scan refuses that owner shape, and here it writes an
    # edge's label on an already-built edge:
    "lionagi.operations.flow:1478:21": "an edge label on a list item, not a class attribute",
    # A class built at runtime by a factory from a parameter; it is outside what the
    # scan proves, and it declares no hash authority:
    "lionagi.protocols.graph.node_factory:144:11": "a node class built by a factory",
    # An assignment to a class authority outside a class body; here it rebinds one
    # connection object's class to a stop-once subclass, on an object the scan does
    # not inventory:
    "lionagi.state.engine:220:9": "a connection object taking a runtime subclass",
}


@dataclass
class _Class:
    node: ast.ClassDef
    location: str
    bases: tuple[str, ...]
    decorators: tuple[tuple[str, ast.expr], ...]


@dataclass
class _Scan:
    classes: dict[str, _Class]
    aliases: dict[str, set[str]]
    symbols: set[str]
    writes: list[tuple[str, str]]
    refusals: list[tuple[str, str]]


def _symbol(node: ast.AST, bindings: dict[str, str], module: str) -> str | None:
    if isinstance(node, ast.Name):
        fallback = (
            f"builtins.{node.id}"
            if node.id in {"object", "setattr", "type"}
            else f"{module}.{node.id}"
        )
        return bindings.get(node.id, fallback)
    if isinstance(node, ast.Attribute):
        parent = _symbol(node.value, bindings, module)
        return f"{parent}.{node.attr}" if parent else None
    if isinstance(node, ast.Subscript):
        return _symbol(node.value, bindings, module)
    if isinstance(node, ast.NamedExpr):
        return _symbol(node.value, bindings, module)
    return None


def _canonicals(symbol: str, aliases: dict[str, set[str]]) -> set[str]:
    """Every symbol ``symbol`` can resolve to through ``aliases``, longest prefix first."""
    resolved: set[str] = set()
    pending: list[tuple[str, frozenset[str]]] = [(symbol, frozenset())]
    seen: set[str] = set()
    while pending:
        current, expanded = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        parts = current.split(".")
        for size in range(len(parts), 0, -1):
            prefix = ".".join(parts[:size])
            targets = aliases.get(prefix, set()) - {prefix}
            if targets:
                if prefix in expanded:
                    resolved.add(current)
                else:
                    pending.extend(
                        (".".join([target, *parts[size:]]), expanded | {prefix})
                        for target in targets
                    )
                break
        else:
            resolved.add(current)
    return resolved or {symbol}


def _none(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def _false(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Constant) and value.value is False


def _plain_dotted(node: ast.AST) -> bool:
    """Whether *node* is a name, or a dotted attribute chain rooted at a name."""
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _plain_dotted(node.value)
    return False


def _readable_base(node: ast.AST) -> bool:
    """Whether a class base is a syntactic form the scan fully reads.

    A plain name or a dotted attribute is read: it resolves to the declaration the
    scan inventories. Anything else is a form it cannot read, so every other base is
    refused -- a subscript such as ``BASES[0]`` or ``Generic[T]``, a starred base, a
    called base, and a constant.
    """
    return _plain_dotted(node)


def _named_keywords(keywords: list[ast.keyword]) -> bool:
    """Whether every keyword is a named one; an unpack (``**mapping``) is not read."""
    return all(keyword.arg is not None for keyword in keywords)


def _readable_decorator(node: ast.AST, bindings: dict[str, str], module: str) -> bool:
    """Whether a class decorator is a form the scan fully reads.

    The decorator must be a plain name or dotted attribute, or a call to one with no
    positional argument and no unpack, and its name must resolve to a symbol bound in
    another module. A decorator this module defines itself is a body the scan does not
    execute, so it is refused rather than read as hash-free; so is a call that could
    pass the keyword that turns on a generated hash unread.
    """
    target = node.func if isinstance(node, ast.Call) else node
    if not _plain_dotted(target):
        return False
    symbol = _symbol(target, bindings, module)
    if not symbol or symbol.startswith(f"{module}."):
        return False
    if isinstance(node, ast.Call):
        return not node.args and _named_keywords(node.keywords)
    return True


def _readable_config(node: ast.AST | None) -> bool:
    """Whether a ``model_config`` value is one of the two forms the scan reads.

    A dict literal whose keys are all strings, and a call with named keyword arguments
    and no positional argument, are read: each entry is a named key the scan can look
    for. A name, a merge, an unpack, a positional argument or any other expression is
    not, because its entries are decided at runtime and could turn on the frozen
    configuration the scan looks for.
    """
    if isinstance(node, ast.Dict):
        return all(
            isinstance(key, ast.Constant) and isinstance(key.value, str) for key in node.keys
        )
    if isinstance(node, ast.Call):
        return not node.args and _named_keywords(node.keywords)
    return False


def _writes_name(node: ast.AST, name: str) -> bool:
    """Whether *node* binds ``name`` anywhere inside it, as a plain name."""
    return any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(node))


def _config_targets(node: ast.stmt) -> list[ast.expr]:
    """The assignment targets of *node*, or an empty list for another statement kind."""
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _contains_attribute(node: ast.AST) -> bool:
    return any(isinstance(item, ast.Attribute) for item in ast.walk(node))


def _own_expressions(node: ast.stmt) -> Iterator[ast.expr]:
    """The expressions a statement evaluates itself, not those of the statements it holds."""
    pending = [child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.stmt)]
    while pending:
        child = pending.pop()
        if isinstance(child, ast.expr):
            yield child
        else:
            # withitem, except handler, match case and pattern, arguments, keyword.
            pending.extend(
                item for item in ast.iter_child_nodes(child) if not isinstance(item, ast.stmt)
            )


# Every statement kind's own suites, by the field names that hold them. A kind
# absent from this table is one the walk cannot classify, and the walk fails closed
# on it rather than reading it as hash-free. ``Match`` holds its suites in ``case``
# clauses, which are not statements, so its bodies are collected separately.
_SUITE_FIELDS: dict[str, tuple[str, ...]] = {
    "FunctionDef": ("body",),
    "AsyncFunctionDef": ("body",),
    "ClassDef": ("body",),
    "For": ("body", "orelse"),
    "AsyncFor": ("body", "orelse"),
    "While": ("body", "orelse"),
    "If": ("body", "orelse"),
    "With": ("body",),
    "AsyncWith": ("body",),
    "Try": ("body", "orelse", "finalbody"),
    "TryStar": ("body", "orelse", "finalbody"),
    "Match": (),
    "Return": (),
    "Delete": (),
    "Assign": (),
    "AugAssign": (),
    "AnnAssign": (),
    "Import": (),
    "ImportFrom": (),
    "Global": (),
    "Nonlocal": (),
    "Expr": (),
    "Pass": (),
    "Break": (),
    "Continue": (),
    "Raise": (),
    "Assert": (),
}


def _suite_bodies(node: ast.stmt, module: str) -> Iterator[list[ast.stmt]]:
    """Every statement body a statement holds, or a refusal for an unknown kind.

    The scan must read a hash write or a frozen configuration wherever it sits, so a
    statement kind it cannot classify is reported rather than skipped: an unknown
    body would otherwise be a place a hash authority could hide unread. The refusal
    names the module and line of the statement it could not read.
    """
    fields = _SUITE_FIELDS.get(type(node).__name__)
    if fields is None:
        raise ValueError(
            f"{module}:{node.lineno}: {type(node).__name__} is a statement kind the "
            "mutable-model scan cannot read; extend the scan before relying on the gate"
        )
    for field in fields:
        body = getattr(node, field)
        if body:
            yield body
    for handler in getattr(node, "handlers", ()) or ():
        if handler.body:
            yield handler.body
    for case in getattr(node, "cases", ()) or ():
        if case.body:
            yield case.body


def _scope_suites(node: ast.stmt, module: str) -> Iterator[list[ast.stmt]]:
    """The suites of *node* that stay in the enclosing scope.

    A function or class definition opens its own scope, so a name assigned there is
    not a class-body setting and is not read as one.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    yield from _suite_bodies(node, module)


def _scan_sources(sources: dict[str, tuple[str, str]]) -> _Scan:
    result = _Scan({}, {}, set(), [], [])
    calls: list[tuple[str, frozenset[str], str, str]] = []
    setattr_calls: list[tuple[str, str, str, ast.Call]] = []
    type_calls: list[tuple[str, str, bool]] = []
    refusals: list[tuple[str, str]] = []
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
                result.aliases.setdefault(f"{scope}.{name}", set()).add(target)

            def assignment(target: ast.AST, value: ast.AST | None) -> None:
                if isinstance(target, ast.Name):
                    if class_name and target.id == "__hash__" and not _none(value):
                        result.writes.append((class_name, "assigned __hash__"))
                    bind(target.id, _symbol(value, bindings, module) or f"{scope}.{target.id}")
                elif isinstance(target, ast.Attribute):
                    if not _plain_dotted(target.value):
                        refusals.append(
                            (
                                f"{module}:{target.lineno}:{target.col_offset + 1}",
                                "an attribute assignment whose owner is not a plain name",
                            )
                        )
                        return
                    if target.attr in _AUTHORITY_ATTRIBUTES:
                        # Reassigning one of these rebinds what the class is after its
                        # declaration, so a class the scan inventories could take another
                        # base or metaclass without any declaration to read.
                        refusals.append(
                            (
                                f"{module}:{target.lineno}:{target.col_offset + 1}",
                                f"an assignment to {target.attr!r} outside a class body",
                            )
                        )
                    elif target.attr == "__hash__":
                        owner = _symbol(target.value, bindings, module)
                        if owner and not _none(value):
                            result.writes.append((owner, "post-definition __hash__ assignment"))
                elif isinstance(target, ast.Starred):
                    assignment(target.value, value)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    # A compound target pairs its elements with the value's by position.
                    # Each element is read the same way, so an attribute whose owner is not
                    # a plain name is refused wherever it sits; a subscript element writes
                    # neither a name nor a class attribute and is left as is.
                    values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
                    for index, child in enumerate(target.elts):
                        if isinstance(child, ast.Subscript):
                            continue
                        assignment(child, values[index] if index < len(values) else None)
                elif isinstance(target, ast.Subscript):
                    # An item assignment writes neither a name nor a class attribute.
                    return

            def loop_target(target: ast.AST, iterable: ast.AST | None) -> None:
                # An attribute target is a binding statement too: a loop or with target
                # that names ``__hash__`` writes it wherever the loop runs.
                if isinstance(target, ast.Attribute):
                    assignment(target, iterable)
                    return
                # A compound or starred target that carries an attribute is refused: the
                # element it writes is not a plain name, so a hash authority in it would
                # go unread. A subscript target writes no class attribute and is left.
                if isinstance(target, (ast.Tuple, ast.List, ast.Set, ast.Starred)):
                    if _contains_attribute(target):
                        refusals.append(
                            (
                                f"{module}:{target.lineno}:{target.col_offset + 1}",
                                "a loop, with or comprehension target the scan cannot read",
                            )
                        )
                        return
                # A name iterating a literal tuple, list or set takes each element in turn;
                # any other loop or with target is a value the scan cannot follow.
                literal = isinstance(target, ast.Name) and isinstance(
                    iterable, (ast.Tuple, ast.List, ast.Set)
                )
                for name in (item for item in ast.walk(target) if isinstance(item, ast.Name)):
                    if not literal:
                        bind(name.id, _UNRESOLVED)
                        continue
                    key = f"{scope}.{name.id}"
                    bindings[name.id] = key
                    for element in iterable.elts:
                        result.aliases.setdefault(key, set()).add(
                            _symbol(element, bindings, module) or _UNRESOLVED
                        )

            for node in body:
                expressions = list(_own_expressions(node))
                for expression in expressions:
                    for item in ast.walk(expression):
                        if isinstance(item, ast.NamedExpr):
                            if (
                                class_name
                                and isinstance(item.target, ast.Name)
                                and item.target.id == "model_config"
                            ):
                                # A configuration bound inside an expression is not a
                                # statement the scan reads as a setting, so the class it
                                # configures could be frozen unread.
                                refusals.append(
                                    (
                                        f"{module}:{item.lineno}:{item.col_offset + 1}",
                                        "a model_config target the scan cannot read",
                                    )
                                )
                            else:
                                assignment(item.target, item.value)
                        elif isinstance(item, ast.comprehension):
                            loop_target(item.target, item.iter)
                inspect_calls(expressions, bindings)
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
                    location = f"{module}:{node.lineno}:{node.col_offset + 1}"
                    for base in node.bases:
                        if not _readable_base(base):
                            refusals.append(
                                (
                                    f"{module}:{base.lineno}:{base.col_offset + 1}",
                                    "a class base the scan cannot read",
                                )
                            )
                    for keyword in node.keywords:
                        if keyword.arg is None:
                            refusals.append(
                                (
                                    f"{module}:{node.lineno}:{node.col_offset + 1}",
                                    "a class keyword the scan cannot read",
                                )
                            )
                        elif keyword.arg == "metaclass":
                            refusals.append(
                                (
                                    location,
                                    "a class whose metaclass the scan cannot inventory",
                                )
                            )
                        elif keyword.arg not in _HASH_KEYWORDS and not isinstance(
                            keyword.value, ast.Constant
                        ):
                            refusals.append(
                                (
                                    location,
                                    f"a class keyword {keyword.arg!r} the scan cannot read",
                                )
                            )
                    for decorator in node.decorator_list:
                        if not _readable_decorator(decorator, bindings, module):
                            refusals.append(
                                (
                                    f"{module}:{decorator.lineno}:{decorator.col_offset + 1}",
                                    "a decorator the scan cannot read",
                                )
                            )
                    for statement in _class_body_statements(node.body, module):
                        for target in _config_targets(statement):
                            if isinstance(target, ast.Name) and target.id == "model_config":
                                if not _readable_config(statement.value):
                                    refusals.append(
                                        (
                                            f"{module}:{statement.lineno}:"
                                            f"{statement.col_offset + 1}",
                                            "a model_config value the scan cannot read",
                                        )
                                    )
                            elif _writes_name(target, "model_config"):
                                # A compound target that binds ``model_config`` as one
                                # element, such as ``(model_config,) = ...``, is a form
                                # the scan cannot read as a single configuration value.
                                refusals.append(
                                    (
                                        f"{module}:{target.lineno}:{target.col_offset + 1}",
                                        "a model_config target the scan cannot read",
                                    )
                                )
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
                    result.classes[fqn] = _Class(node, location, bases, decorators)
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
                elif isinstance(node, ast.Delete):
                    for target in node.targets:
                        if not isinstance(target, ast.Attribute):
                            continue
                        if not _plain_dotted(target.value):
                            refusals.append(
                                (
                                    f"{module}:{target.lineno}:{target.col_offset + 1}",
                                    "an attribute deletion whose owner is not a plain name",
                                )
                            )
                            continue
                        if target.attr == "__hash__":
                            owner = _symbol(target.value, bindings, module)
                            if owner:
                                result.writes.append((owner, "deleted __hash__"))
                elif not isinstance(node, ast.Expr):
                    if isinstance(node, (ast.For, ast.AsyncFor)):
                        loop_target(node.target, node.iter)
                    elif isinstance(node, (ast.With, ast.AsyncWith)):
                        for item in node.items:
                            if item.optional_vars is not None:
                                loop_target(item.optional_vars, None)
                    # Inspect declarations in conditional, loop, try and match suites
                    # without pretending to decide which branch executes at runtime.
                    for statements in _suite_bodies(node, module):
                        walk(statements, bindings.copy(), scope, class_name, closure)

        def inspect_calls(expressions: list[ast.expr], bindings: dict[str, str]) -> None:
            for expression in expressions:
                for call in (item for item in ast.walk(expression) if isinstance(item, ast.Call)):
                    function = _symbol(call.func, bindings, module)
                    if not function and isinstance(call.func, ast.Call):
                        # ``type(X)(...)``: the meta-type the inner call returns builds the
                        # class, so the inner call's function is what identifies it.
                        function = _symbol(call.func.func, bindings, module)
                    canonical = _canonicals(function, result.aliases) if function else frozenset()
                    location = f"{module}:{call.lineno}:{call.col_offset + 1}"
                    # ``type(value)`` reads a type, which is not a class the scan has
                    # to inventory, and the meta-type it returns may be called as an
                    # ordinary constructor. A three-argument call builds a class, as
                    # does any other argument shape the scan cannot read: an unpacked
                    # or starred argument, a keyword argument, and a call through a
                    # re-exported name. The name is resolved after every module is
                    # scanned, so a re-exported ``type`` is caught even though its alias
                    # is not yet known here.
                    if isinstance(call.func, ast.Call):
                        # ``type(X)(...)``: the returned meta-type builds the class,
                        # so a three-argument call of it is a class the scan cannot
                        # inventory.
                        builds_a_class = len(call.args) == 3
                    else:
                        # One plain argument reads a type; an unpacked argument, a
                        # keyword argument and any other count are refused, since the
                        # class-building form is a three-argument call.
                        builds_a_class = (
                            len(call.args) != 1
                            or bool(call.keywords)
                            or isinstance(call.args[0], ast.Starred)
                        )
                    if builds_a_class:
                        type_calls.append((function, location, True))
                    if "builtins.type" in canonical:
                        continue
                    writes_attribute = (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr in _DELATTR_ATTRIBUTES
                        and len(call.args) == 3
                        and isinstance(call.args[1], ast.Constant)
                        and isinstance(call.args[1].value, str)
                    )
                    if not function and not writes_attribute:
                        continue
                    owner = _symbol(call.args[0], bindings, module) if call.args else None
                    if writes_attribute and not (canonical & _SETATTR):
                        # A call through an attribute-writing name whose function the
                        # scan cannot resolve, such as ``type(Known).__setattr__(...)``,
                        # is refused: the class it writes may be one the scan
                        # inventories and the write would otherwise go unread.
                        refusals.append((location, "a setattr call the scan cannot read"))
                        continue
                    # A setattr-like call is resolved after every module is scanned, so a
                    # re-exported name still resolves. The owner and the location are
                    # taken here, where the bindings are.
                    setattr_calls.append((function, location, owner or "", call))
                    if call.args and any(
                        kw.arg in _HASH_KEYWORDS and not _false(kw.value) for kw in call.keywords
                    ):
                        if owner:
                            calls.append(
                                (
                                    function,
                                    frozenset({"dataclasses.dataclass"}),
                                    owner,
                                    "post-definition dataclass hash generation",
                                )
                            )

        walk(tree.body, {}, module)
    for function, location, builds_a_class in type_calls:
        if not builds_a_class or not function:
            continue
        if "builtins.type" in _canonicals(function, result.aliases):
            refusals.append((location, "a class built with type()"))
    for function, expected, owner, description in calls:
        if _canonicals(function, result.aliases) & expected:
            result.writes.append((owner, description))
    for function, location, owner, call in setattr_calls:
        if not (_canonicals(function, result.aliases) & _SETATTR):
            continue
        first = call.args[0] if call.args else None
        second = call.args[1] if len(call.args) >= 2 else None
        if (
            len(call.args) != 3
            or not isinstance(first, (ast.Name, ast.Attribute))
            or not isinstance(second, ast.Constant)
            or not isinstance(second.value, str)
        ):
            refusals.append((location, "a setattr call the scan cannot read"))
        elif second.value == "__hash__" and owner and not _none(call.args[2]):
            result.writes.append((owner, "setattr __hash__"))
        elif owner:
            result.writes.append((owner, "setattr"))
    result.refusals = refusals
    return result


def _selected(scan: _Scan, roots: frozenset[str] = _ROOTS) -> frozenset[str]:
    selected = set(roots & scan.classes.keys())
    while True:
        extended = selected | {
            name
            for name, declaration in scan.classes.items()
            if any(_canonicals(base, scan.aliases) & selected for base in declaration.bases)
        }
        if extended == selected:
            return frozenset(selected)
        selected = extended


def _frozen_setting(targets: list[ast.expr], value: ast.expr | None, name: str) -> bool:
    if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
        return False
    if name == "frozen":
        return value is not None and not _false(value)
    if isinstance(value, ast.Call):
        return any(kw.arg == "frozen" and not _false(kw.value) for kw in value.keywords)
    if isinstance(value, ast.Dict):
        return any(
            isinstance(key, ast.Constant) and key.value == "frozen" and not _false(item)
            for key, item in zip(value.keys, value.values)
        )
    return False


def _settings(
    body: list[ast.stmt], module: str
) -> Iterator[tuple[list[ast.expr], ast.expr | None]]:
    for node in body:
        for target in _config_targets(node):
            yield [target], node.value
        for nested in _scope_suites(node, module):
            yield from _settings(nested, module)


def _class_body_statements(body: list[ast.stmt], module: str) -> Iterator[ast.stmt]:
    """Every statement in a class body, nested suites included, without entering a
    new scope."""
    for node in body:
        yield node
        for nested in _scope_suites(node, module):
            yield from _class_body_statements(nested, module)


def _generation(declaration: _Class, aliases: dict[str, set[str]], module: str) -> bool:
    if any(kw.arg == "frozen" and not _false(kw.value) for kw in declaration.node.keywords):
        return True
    for symbol, decorator in declaration.decorators:
        if "dataclasses.dataclass" in _canonicals(symbol, aliases) and isinstance(
            decorator, ast.Call
        ):
            if any(
                kw.arg in {"unsafe_hash", "frozen"} and not _false(kw.value)
                for kw in decorator.keywords
            ):
                return True
    for targets, value in _settings(declaration.node.body, module):
        if _frozen_setting(targets, value, "model_config"):
            return True
    for node in _class_body_statements(declaration.node.body, module):
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            for targets, value in _settings(node.body, module):
                if _frozen_setting(targets, value, "frozen"):
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
        for resolved in _canonicals(owner, scan.aliases) & selected:
            violations.append(f"{resolved}: {description}")
    for location, reason in scan.refusals:
        if location not in _ALLOWLIST:
            violations.append(f"{location}: {reason}")
    for name, declaration in scan.classes.items():
        if any(_UNRESOLVED in _canonicals(base, scan.aliases) for base in declaration.bases):
            violations.append(
                f"{declaration.location}: {name} takes a base bound by a loop or with "
                "target the scan cannot resolve"
            )
    for name in selected:
        if _generation(scan.classes[name], scan.aliases, scan.classes[name].location):
            violations.append(f"{name}: generated hash or frozen configuration")
        ancestors = set()
        pending = list(scan.classes[name].bases)
        while pending:
            for ancestor in _canonicals(pending.pop(), scan.aliases):
                if ancestor in ancestors or ancestor not in scan.classes:
                    continue
                ancestors.add(ancestor)
                pending.extend(scan.classes[ancestor].bases)
                if ancestor not in selected and _generation(
                    scan.classes[ancestor], scan.aliases, scan.classes[ancestor].location
                ):
                    violations.append(f"{name}: inherited generated hash authority from {ancestor}")
        for owner, description in scan.writes:
            for resolved in _canonicals(owner, scan.aliases) & (ancestors - selected):
                violations.append(
                    f"{name}: inherited hash authority from {resolved} ({description})"
                )
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
        (
            "from lionagi.models import HashableModel\nif (Base := HashableModel):\n    pass\nclass Added(Base): pass",
            "sample.Added",
        ),
        (
            "from lionagi.models import HashableModel\n_ = [(Base := HashableModel) for _ in (1,)]\nclass Added(Base): pass",
            "sample.Added",
        ),
        (
            "from lionagi.models import HashableModel\nAlias = (Base := HashableModel)\nclass Added(Base): pass",
            "sample.Added",
        ),
        (
            "from lionagi.models import HashableModel\nAlias = (Base := HashableModel)\nclass Added(Alias): pass",
            "sample.Added",
        ),
        (
            "from lionagi.models import HashableModel\nmatch 1:\n    case _ if (Base := HashableModel):\n        pass\nclass Added(Base): pass",
            "sample.Added",
        ),
        (
            "from lionagi.models import HashableModel\nfor Base in (object, HashableModel):\n    class Added(Base): pass",
            "sample.Added",
        ),
        (
            "try:\n    from lionagi.models import HashableModel as Base\nexcept ImportError:\n    Base = object\nclass Added(Base): pass",
            "sample.Added",
        ),
    ],
    ids=[
        "assignment",
        "module-attribute",
        "function-alias",
        "function-import",
        "class-alias",
        "transitive",
        "walrus-if",
        "walrus-comprehension",
        "walrus-value",
        "walrus-result",
        "walrus-guard",
        "literal-loop",
        "except-fallback",
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
    assert _canonicals("a.Member", {"a": {"b"}, "b": {"a"}}) == {"a.Member"}
    assert _canonicals("a.Member", {"a": {"a.Nested"}}) == {"a.Nested.Member"}


def test_a_name_bound_twice_resolves_to_both_values():
    assert _canonicals("x.Member", {"x": {"p", "q"}}) == {"p.Member", "q.Member"}


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
        "while flag:\n    {}",
        "try:\n    pass\nexcept ImportError:\n    pass\nelse:\n    {}",
        pytest.param(
            "try:\n    pass\nexcept* ImportError:\n    {}",
            marks=pytest.mark.skipif(sys.version_info < (3, 11), reason="except* is 3.11 syntax"),
            id="except-star",
        ),
        "async def make():\n    async with context:\n        {}",
    ],
    ids=[
        "if",
        "else",
        "except",
        "finally",
        "with",
        "for",
        "case",
        "later-case",
        "while",
        "try-else",
        "except-star",
        "async-with",
    ],
)
def test_a_descendant_declared_inside_any_suite_is_inventoried(suite):
    declaration = "class Hidden(HashableModel): pass"
    if suite.startswith("async def"):
        declaration = declaration.replace("\n", "\n    ")
    scan = _sample("from lionagi.models import HashableModel\n" + suite.format(declaration))
    hidden = "sample.make.<locals>.Hidden" if suite.startswith("async def") else "sample.Hidden"
    assert _violations(scan, {*_ROOTS}) == [f"inventory drift: missing=[], unexpected=['{hidden}']"]


@pytest.mark.parametrize(
    "body",
    [
        "    match flag:\n        case 1:\n            __hash__ = object.__hash__",
        "    match flag:\n        case 1:\n            def __hash__(self): return 1",
        "    match flag:\n        case 1:\n            model_config = {'frozen': True}",
        "    match flag:\n        case 1:\n            model_config = ConfigDict(frozen=True)",
        "    match flag:\n        case 1:\n            class Config:\n                frozen = True",
        "    if flag:\n        __hash__ = object.__hash__",
        "    if flag:\n        model_config = {'frozen': True}",
        "    for _ in ():\n        __hash__ = object.__hash__",
        "    while flag:\n        __hash__ = object.__hash__\n        break",
        "    with context:\n        __hash__ = object.__hash__",
        "    try:\n        __hash__ = object.__hash__\n    except Exception:\n        pass",
        "    try:\n        pass\n    finally:\n        __hash__ = object.__hash__",
    ],
)
def test_hash_authority_inside_any_suite_is_rejected(body):
    scan = _sample(
        "from lionagi.models import HashableModel\n"
        "from pydantic import ConfigDict\n"
        "class Known(HashableModel):\n" + body
    )
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors and all("sample.Known" in error for error in errors), errors


@pytest.mark.parametrize(
    "statement",
    [
        "for Known.__hash__ in (object.__hash__,):\n    pass",
        "with context() as Known.__hash__:\n    pass",
        "_ = [None for Known.__hash__ in (object.__hash__,)]",
    ],
    ids=["for-target", "with-target", "comprehension-target"],
)
def test_a_hash_written_as_a_binding_target_is_rejected(statement):
    """A loop, with or comprehension target binds an attribute just as an assignment
    does, so a target that names ``__hash__`` is a write."""

    scan = _sample(
        "from lionagi.models import HashableModel\nclass Known(HashableModel): pass\n" + statement
    )
    assert "sample.Known: post-definition __hash__ assignment" in (
        _violations(scan, {*_ROOTS, "sample.Known"})
    )


def test_an_unknown_statement_kind_fails_closed():
    """A statement the scan cannot classify must not be read as hash-free."""

    class Unknown(ast.stmt):
        _fields = ("body",)
        lineno = 1

        def __init__(self, body):
            self.body = body

    tree = ast.parse("class Known: pass\n")
    unknown = Unknown([ast.parse("Known.__hash__ = object.__hash__\n").body[0]])
    tree.body.append(unknown)
    original = ast.parse

    def parse_with_unknown(source, *args, **kwargs):
        if kwargs.get("filename") == "sample" or (args and args[0] == "sample"):
            return tree
        return original(source, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ast, "parse", parse_with_unknown)
        with pytest.raises(
            ValueError,
            match=r"^sample:1: Unknown is a statement kind the mutable-model scan cannot read",
        ):
            _sample("")


@pytest.mark.parametrize(
    "body",
    [
        "    def __hash__(self): return 1",
        "    __hash__ = object.__hash__",
        "    __hash__: object = object.__hash__",
        "    model_config = {'frozen': True}",
        "    model_config = ConfigDict(frozen=True)",
        "    class Config:\n        frozen = True",
        "    class Config:\n        frozen: bool = True",
        "    model_config: dict = {}\n    model_config |= {'frozen': True}",
        "    model_config: dict = {}\n    model_config |= ConfigDict(frozen=True)",
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
        "if setattr(Known, '__hash__', object.__hash__):\n    pass",
        "while setattr(Known, '__hash__', object.__hash__):\n    break",
        "with setattr(Known, '__hash__', object.__hash__):\n    pass",
        "def change():\n    return setattr(Known, '__hash__', object.__hash__)",
        "assert not setattr(Known, '__hash__', object.__hash__)",
        "for _ in [setattr(Known, '__hash__', object.__hash__)]:\n    pass",
        "match setattr(Known, '__hash__', object.__hash__):\n    case _:\n        pass",
        "match 1:\n    case _ if setattr(Known, '__hash__', object.__hash__):\n        pass",
        "type.__setattr__(Known, '__hash__', object.__hash__)",
    ],
)
def test_post_definition_hash_authority_is_rejected(assignment):
    scan = _sample(
        "from lionagi.models import HashableModel\nclass Known(HashableModel): pass\n" + assignment
    )
    assert _violations(scan, {*_ROOTS, "sample.Known"})


@pytest.mark.parametrize(
    "source",
    [
        "for Base in bases():\n    class Added(Base): pass",
        "with context() as Base:\n    class Added(Base): pass",
    ],
    ids=["loop", "with"],
)
def test_a_base_bound_by_a_target_the_scan_cannot_resolve_fails_the_gate(source):
    scan = _sample("from lionagi.models import HashableModel\n" + source)
    assert (
        "sample:3:5: sample.Added takes a base bound by a loop or with target the scan "
        "cannot resolve"
    ) in (_violations(scan, {*_ROOTS}))


@pytest.mark.parametrize(
    "source",
    [
        "Added = type('Added', (HashableModel,), {})",
        "Known = type('Known', (HashableModel,), {'__hash__': object.__hash__})",
        "class Parent(HashableModel): pass\nAdded = type('Added', [Parent], {})",
        "BASES = (HashableModel,)\nK = type('K', BASES, {'__hash__': object.__hash__})",
        "Sub = type(HashableModel)('Sub', (HashableModel,), {'__hash__': object.__hash__})",
    ],
    ids=[
        "bare",
        "with-hash",
        "from-a-descendant",
        "named-bases",
        "type-of-family-member",
    ],
)
def test_a_class_created_at_runtime_fails_the_gate(source):
    """A class built by ``type()``, directly or from a type the family member returns,
    is not a declaration the scan can inventory, whether or not it is a family member:
    the gate fails closed on every such call."""
    scan = _sample("from lionagi.models import HashableModel\n" + source)
    errors = _violations(scan, {*_ROOTS, "sample.Parent"} if "Parent" in source else {*_ROOTS})
    assert any("a class built with type()" in error for error in errors), errors


def test_a_type_call_that_does_not_build_a_class_is_not_refused():
    scan = _sample("from lionagi.models import HashableModel\nkind = type(HashableModel)")
    # ``type(HashableModel)`` reads a type, it does not build one, so it is not refused.
    assert _violations(scan, {*_ROOTS}) == []


def test_a_class_built_with_type_outside_the_family_is_refused():
    scan = _sample("from lionagi.models import HashableModel\nPlain = type('Plain', (object,), {})")
    assert [error for error in _violations(scan, {*_ROOTS}) if "a class built with type()" in error]


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


_UNREADABLE_SHAPES = [
    (
        "S01",
        "BASES = (HashableModel,)\nclass K(BASES[0]):\n    __hash__ = object.__hash__\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "S02",
        "class Known(HashableModel):\n    model_config = ConfigDict({'frozen': True})\n",
    ),
    (
        "S03",
        "class Known(HashableModel):\n    model_config = dict([('frozen', True)])\n",
    ),
    (
        "S04",
        "class Known(HashableModel, **{'frozen': True}):\n    pass\n",
    ),
    (
        "S05",
        "FROZEN = {'unsafe_hash': True}\n@dataclass(**FROZEN)\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "S06",
        "@dataclass(**{'unsafe_hash': True})\nclass Known(HashableModel):\n    pass\n",
    ),
    (
        "S07",
        "class Known(HashableModel):\n    (model_config,) = ({'frozen': True},)\n",
    ),
    (
        "S08",
        "CFG = {'frozen': True}\nclass Known(HashableModel):\n    (model_config,) = (CFG,)\n",
    ),
    (
        "S09",
        "class Known(HashableModel):\n    _ = (model_config := {'frozen': True})\n",
    ),
    (
        "S10",
        "class Known(HashableModel):\n    pass\nsetattr([Known][0], '__hash__', object.__hash__)\n",
    ),
    (
        "S11",
        "class Known(HashableModel):\n    pass\n"
        "setattr(globals()['Known'], '__hash__', object.__hash__)\n",
    ),
    (
        "S12",
        "class Known(HashableModel):\n    pass\n"
        "type(Known).__setattr__(Known, '__hash__', object.__hash__)\n",
    ),
    (
        "S13",
        "K = type(*('K', (HashableModel,), {'__hash__': object.__hash__}))\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "S14",
        "K = type('K', *((HashableModel,), {'__hash__': object.__hash__}))\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "S15",
        "def add_hash(cls):\n    cls.__hash__ = object.__hash__\n    return cls\n"
        "@add_hash\nclass Known(HashableModel):\n    pass\n",
    ),
    (
        "S16",
        "class Plain:\n    __hash__ = object.__hash__\nPlain.__bases__ = (HashableModel,)\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "S17",
        "class Known(HashableModel):\n    pass\ndel [Known][0].__hash__\n",
    ),
    (
        "S18",
        "from helpers import maker\n"
        "K = maker('K', (HashableModel,), {'__hash__': object.__hash__})\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "S19",
        "class Meta(type):\n    pass\nclass Known(HashableModel):\n    pass\n"
        "Known.__class__ = Meta\n",
    ),
]
# A helper module the shapes that follow a re-export need, so a re-exported name is
# resolved after every module is scanned.
_UNREADABLE_SHAPE_MODULES = {"S18": {"helpers": ("", "from builtins import type as maker\n")}}


@pytest.mark.parametrize(
    "case",
    _UNREADABLE_SHAPES,
    ids=[shape[0] for shape in _UNREADABLE_SHAPES],
)
def test_a_class_shape_the_scan_cannot_read_is_reported(case):
    """Every class declaration, decorator, ``model_config`` assignment and class-writing
    call whose form the scan does not fully read is reported, not silently accepted. A
    reported shape fails the gate unless it is one of the audited production sites.
    """

    name, snippet = case
    source = (
        "from lionagi.models import HashableModel\nfrom pydantic import ConfigDict\n"
        "from dataclasses import dataclass\n" + snippet
    )
    scan = _sample(source, _UNREADABLE_SHAPE_MODULES.get(name))
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors, f"shape was neither reported nor allowlisted: {snippet!r}"
    assert not any(error in _ALLOWLIST for error in errors), errors
    assert all(error.split(": ", 1)[0].count(":") == 2 for error in errors), errors


_SCAN_REFUSAL_CASES = [
    (
        "C01",
        "class Known(HashableModel):\n    pass\n"
        "for (Known.__hash__, _) in [(object.__hash__, 0)]:\n    pass\n",
    ),
    (
        "C02",
        "class Known(HashableModel):\n    pass\n"
        "for Known.__hash__, _ in [(object.__hash__, 0)]:\n    pass\n",
    ),
    (
        "C03",
        "from contextlib import nullcontext\nclass Known(HashableModel):\n    pass\n"
        "with nullcontext((object.__hash__, 0)) as (Known.__hash__, _):\n    pass\n",
    ),
    (
        "C04",
        "class Known(HashableModel):\n    pass\n"
        "_ = [None for (Known.__hash__, _) in [(object.__hash__, 0)]]\n",
    ),
    (
        "C05",
        "class Known(HashableModel):\n    pass\n"
        "for [Known.__hash__] in [[object.__hash__]]:\n    pass\n",
    ),
    (
        "C06",
        "class Known(HashableModel):\n    pass\n"
        "for Known.__hash__ in (object.__hash__,):\n    pass\n",
    ),
    (
        "C07",
        "class Known(HashableModel):\n    pass\nsetattr(*(Known, '__hash__', object.__hash__))\n",
    ),
    (
        "C08",
        "class Known(HashableModel):\n    pass\nsetattr(Known, '__hash__', object.__hash__)\n",
    ),
    (
        "C09",
        "CFG = {'frozen': True}\nclass Known(HashableModel):\n    model_config = CFG\n",
    ),
    (
        "C10",
        "BASE = {'populate_by_name': True}\nclass Known(HashableModel):\n"
        "    model_config = BASE | {'frozen': True}\n",
    ),
    (
        "C11",
        "BASE = {'frozen': True}\nclass Known(HashableModel):\n    model_config = {**BASE}\n",
    ),
    (
        "C12",
        "class Known(HashableModel):\n    model_config = {'frozen': True}\n",
    ),
    (
        "C13",
        "FROZEN = {'frozen': True}\nclass Known(HashableModel):\n"
        "    model_config = ConfigDict(**FROZEN)\n",
    ),
    (
        "C14",
        "class Meta(type):\n    def __new__(m, n, b, d):\n"
        "        d['__hash__'] = object.__hash__\n"
        "        return super().__new__(m, n, b, d)\n"
        "class Known(HashableModel, metaclass=Meta):\n    pass\n",
    ),
    (
        "C15",
        "BASES = (HashableModel,)\nK = type('K', BASES, {'__hash__': object.__hash__})\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "C16",
        "K = type('K', (HashableModel,), {'__hash__': object.__hash__})\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "C17",
        "BASES = (HashableModel,)\nclass K(*BASES):\n    __hash__ = object.__hash__\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "C18",
        "Sub = type(HashableModel)('Sub', (HashableModel,), {'__hash__': object.__hash__})\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "C19",
        "class Known(HashableModel):\n    pass\nflag = 1\nmatch flag:\n"
        "    case _ if setattr(Known, '__hash__', object.__hash__):\n        pass\n",
    ),
    (
        "C20",
        "class Known(HashableModel):\n    pass\ntry:\n    pass\n"
        "except (setattr(Known, '__hash__', object.__hash__) or Exception):\n    pass\n",
    ),
    (
        "C21",
        "class Known(HashableModel):\n    pass\nflag = 1\nmatch flag:\n"
        "    case 1:\n        Known.__hash__ = object.__hash__\n",
    ),
    (
        "C22",
        "class Known(HashableModel):\n    pass\ntype Alias = int\n",
    ),
    (
        "C23",
        "class Known(HashableModel):\n    type Alias = int\n",
    ),
]


@pytest.mark.parametrize(
    "snippet",
    [case[1] for case in _SCAN_REFUSAL_CASES],
    ids=[case[0] for case in _SCAN_REFUSAL_CASES],
)
def test_a_shape_the_scan_cannot_read_is_refused_or_allowlisted(snippet):
    """Every shape the scan cannot resolve is refused, naming the file and line, unless it
    is listed in the test's allowlist with a reason. A statement kind the walk does not
    know is refused outright. The family's own hash authority is refused by the rules
    that name it."""
    source = "from lionagi.models import HashableModel\nfrom pydantic import ConfigDict\n" + snippet
    try:
        scan = _sample(source)
    except ValueError as refused:
        assert "statement kind the mutable-model scan cannot read" in str(refused)
        return
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors, f"shape was neither refused nor allowlisted: {snippet!r}"


def test_the_scan_allowlist_names_exactly_the_production_refusals_with_a_reason():
    """The allowlist and the production refusals name the same locations: a site that
    is no longer refused must leave the allowlist, and a new refusal must not pass
    silently. Each key is ``module:line:column`` with a reason.
    """
    assert _ALLOWLIST, "the allowlist must carry the audited production shapes"
    for location, reason in _ALLOWLIST.items():
        assert location.count(":") == 2, location
        assert reason.strip(), location
    assert set(_ALLOWLIST) == {location for location, _ in _production().refusals}
