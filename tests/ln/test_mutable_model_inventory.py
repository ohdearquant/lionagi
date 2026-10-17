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
dotted attribute whose resolved symbol is a declared class, a builtin class, an imported
symbol or ``Generic``; class keywords, decorator keywords and ``model_config`` entries
written as named keyword arguments; a ``model_config`` value given as a dict literal with
string keys and constant values, or as a call to ``ConfigDict`` or ``dict`` whose own
arguments are named keyword arguments with constant values; and a ``model_config`` assigned
to a single plain name. A decorator is read when its resolved symbol is one of the audited
names on the allow-list. A class body binds ``__hash__``, ``__eq__``, ``model_config`` or
``Config`` only in a form the scan fully reads, and every other binding of one of those
names is refused. Everything else is a "cannot read" violation, naming the file and line: a
statement kind the walk does not know, a class base, class keyword or decorator outside
those forms, an attribute assignment whose owner is not a plain name, an assignment to
``__bases__``, ``__class__`` or ``__mro__``, an assignment outside a class body to
``__hash__``, ``__eq__`` or ``model_config``, a ``del`` of an attribute whose owner is not a
plain name, a class built by a ``type(...)`` call, and a ``setattr``, ``delattr`` or
``type(X).__setattr__`` whose owner or attribute is not a literal the scan reads. Each is
refused unless it is listed in the test's allowlist with a reason. A class a function builds
from one of its parameters, or one ``create_model`` builds, is outside what the scan proves,
as the inventory's scope limits say. Consumer references
document audited boundaries rather than arbitrary dataflow. Wildcard imports are
rejected because they can conceal declared descendants. Runtime container and mutation
contracts cover the inventoried concrete types.
"""

from __future__ import annotations

import ast
import builtins
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
    {
        "builtins.setattr",
        "builtins.delattr",
        "builtins.type.__setattr__",
        "builtins.type.__delattr__",
        "builtins.object.__setattr__",
    }
)
# The attribute-writing names whose function the scan resolves: a call through one of
# them that writes an attribute name the scan cannot read is refused rather than skipped.
_DELATTR_ATTRIBUTES = frozenset({"__setattr__", "__delattr__"})
# Assigning one of these rebinds what a class is or the configuration it inherits, so a
# class the scan inventories could take a new base, metaclass or configuration after its
# declaration: every such write is refused.
_CLASS_AUTHORITY = frozenset({"__bases__", "__class__", "__mro__", "model_config"})
# The names a class body may bind as hash authority: each changes equality or the
# configuration a subclass inherits, so every binding of one is read in a form the scan
# fully understands or refused.
_AUTHORITY_NAMES = frozenset({"__hash__", "__eq__", "model_config", "Config"})
# The keyword names that turn on a generated hash or a frozen configuration. The
# scan decides these by their value; every other named keyword is read as not one.
_HASH_KEYWORDS = frozenset({"unsafe_hash", "frozen"})
# The resolved names a ``model_config`` value may be: a call to one of these with named
# constant keywords, or a ``dict`` literal with string keys and constant values, is read.
_CONFIG_CALLS = frozenset({"pydantic.ConfigDict", "builtins.dict"})
# The resolved names a class decorator may be. A decorator not on this allow-list -- one a
# module defines itself, or re-exports under a new name -- is refused rather than read as
# hash-free, so a name is trusted only when it resolves through the module's own imports to
# one of these declarations.
_CLASS_DECORATORS = frozenset(
    {
        "dataclasses.dataclass",
        "typing.runtime_checkable",
        # Each provider config member registers an endpoint class through its own
        # ``register`` and returns the class unchanged.
        "lionagi.providers.ag2._config.AG2Configs.AGENT.register",
        "lionagi.providers.ag2._config.AG2Configs.GROUP_CHAT.register",
        "lionagi.providers.ag2._config.AG2Configs.NLIP.register",
        "lionagi.providers.anthropic._config.AnthropicConfigs.MESSAGES.register",
        "lionagi.providers.anthropic._config.ClaudeCodeConfigs.CLI.register",
        "lionagi.providers.deepseek._config.DeepSeekConfigs.CHAT.register",
        "lionagi.providers.exa._config.ExaConfigs.CONTENTS.register",
        "lionagi.providers.exa._config.ExaConfigs.FIND_SIMILAR.register",
        "lionagi.providers.exa._config.ExaConfigs.SEARCH.register",
        "lionagi.providers.firecrawl._config.FirecrawlConfigs.CRAWL.register",
        "lionagi.providers.firecrawl._config.FirecrawlConfigs.MAP.register",
        "lionagi.providers.firecrawl._config.FirecrawlConfigs.SCRAPE.register",
        "lionagi.providers.google._config.GeminiChatConfigs.CHAT.register",
        "lionagi.providers.google._config.GeminiCodeConfigs.CLI.register",
        "lionagi.providers.groq._config.GroqConfigs.AUDIO_TRANSCRIPTION.register",
        "lionagi.providers.groq._config.GroqConfigs.CHAT.register",
        "lionagi.providers.lattice._config.LatticeConfigs.CHAT.register",
        "lionagi.providers.lattice._config.LatticeConfigs.EMBED.register",
        "lionagi.providers.nvidia_nim._config.NvidiaNimConfigs.CHAT.register",
        "lionagi.providers.nvidia_nim._config.NvidiaNimConfigs.EMBED.register",
        "lionagi.providers.ollama._config.OllamaConfigs.CHAT.register",
        "lionagi.providers.ollama._config.OllamaConfigs.EMBED.register",
        "lionagi.providers.ollama._config.OllamaConfigs.GENERATE.register",
        "lionagi.providers.openai._config.CodexConfigs.CLI.register",
        "lionagi.providers.openai._config.OpenAIConfigs.AUDIO_SPEECH.register",
        "lionagi.providers.openai._config.OpenAIConfigs.AUDIO_TRANSCRIPTION.register",
        "lionagi.providers.openai._config.OpenAIConfigs.BATCH.register",
        "lionagi.providers.openai._config.OpenAIConfigs.CHAT.register",
        "lionagi.providers.openai._config.OpenAIConfigs.EMBED.register",
        "lionagi.providers.openai._config.OpenAIConfigs.IMAGE_EDIT.register",
        "lionagi.providers.openai._config.OpenAIConfigs.IMAGE_GENERATION.register",
        "lionagi.providers.openai._config.OpenAIConfigs.RESPONSE.register",
        "lionagi.providers.openrouter._config.OpenRouterConfigs.CHAT.register",
        "lionagi.providers.perplexity._config.PerplexityConfigs.CHAT.register",
        "lionagi.providers.pi._config.PiConfigs.CLI.register",
        "lionagi.providers.tavily._config.TavilyConfigs.EXTRACT.register",
        "lionagi.providers.tavily._config.TavilyConfigs.SEARCH.register",
        "lionagi.service.connections.registry.register_endpoint",
    }
)


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
    "lionagi.service.connections.endpoint_config:207:17": "a config key set by name",
    "lionagi.service.connections.mcp_wrapper:76:9": "a probe attribute set on an exception",
    # A class base the scan cannot read: a base subscripted by a type parameter. Each
    # declares no hash authority of its own; the refused node is the subscript.
    "lionagi.adapters._base:331:15": "``Protocol[T]``, a protocol parameterized by a type",
    "lionagi.adapters._base:508:20": "``Protocol[T]``, a protocol parameterized by a type",
    "lionagi.adapters.async_postgres_adapter:21:39": "``AsyncPostgresAdapter[T]``, an imported adapter",
    "lionagi.adapters.csv_:26:31": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.adapters.json_:26:32": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.adapters.pandas_:37:37": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.adapters.toml_:33:32": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.ln._cache:19:23": "``Generic[K, V]``, the typing generic base",
    "lionagi.ln.concurrency.primitives:126:13": "``Generic[T]``, the typing generic base",
    "lionagi.protocols._concepts:110:21": "``Generic[E]``, the typing generic base",
    "lionagi.protocols._concepts:98:23": "``Generic[E]``, the typing generic base",
    "lionagi.protocols.generic.element:274:10": "``Generic[E]``, the typing generic base",
    "lionagi.protocols.generic.flow:25:21": "``Generic[E, P]``, the typing generic base",
    "lionagi.protocols.generic.pile:175:21": "``Collective[T]``, the collection base on ``Element``",
    "lionagi.protocols.generic.pile:175:36": "``Generic[T]``, the typing generic base",
    "lionagi.protocols.generic.progression:139:28": "``Ordering[T]``, the ordering base on ``Element``",
    "lionagi.protocols.generic.progression:139:41": "``Generic[T]``, the typing generic base",
    "lionagi.protocols.graph.graph:33:34": "``Generic[T]``, the typing generic base",
    # A metaclass the scan does not inventory; the class declares no hash authority
    # and is outside the mutable family:
    "lionagi.ln.types._sentinel:38:1": "a singleton metaclass, not a mutable model",
    # An attribute assignment whose owner is an item of a list, not a name or a
    # dotted attribute; the scan refuses that owner shape, and here it writes an
    # edge's label on an already-built edge:
    "lionagi.operations.flow:1547:21": "an edge label on a list item, not a class attribute",
    # A class built at runtime by a factory from a parameter; it is outside what the
    # scan proves, and it declares no hash authority:
    "lionagi.protocols.graph.node_factory:144:11": "a node class built by a factory",
    # An assignment to a class authority outside a class body; here it rebinds one
    # connection object's class to a stop-once subclass, on an object the scan does
    # not inventory:
    "lionagi.state.engine:238:9": "a connection object taking a runtime subclass",
    # A class whose base is a function parameter: the scan cannot see the class the
    # parameter holds, and each declares no hash authority of its own:
    "lionagi.state.engine:176:30": "a subclass of a caller-supplied connection class",
    "lionagi.testing.helpers:227:22": "a subclass of the class a classmethod is called on",
    # A ``model_config`` the scan cannot read in full: ``SettingsConfigDict`` is the
    # settings analogue of ``ConfigDict``, and the entry below passes a callable rather
    # than a constant. Neither sets a frozen configuration, so neither class hashes:
    "lionagi.config:168:5": "a pydantic settings config, which sets no frozen option",
    "lionagi.studio.operator.types:48:5": "a config whose alias generator is a callable",
    # An attribute assignment whose owner is not a plain name; here each increments a
    # counter field on an object held in a dict slot:
    "lionagi.studio.scheduler.signals:219:17": "a counter field on a dict-held object",
    "lionagi.service.accounts._router:588:13": (
        "a counter on a held character popped from a list, not a class attribute"
    ),
    "lionagi.state.compat:1702:13": (
        "a frozen dataclass folding its own string fields in __post_init__"
    ),
    "lionagi.studio.scheduler.signals:224:17": "a counter field on a dict-held object",
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
        # A module that never binds a name it uses reads a builtin of that name, so the
        # name resolves to the builtin rather than to a module-local binding that does
        # not exist. A name the module does bind keeps its own binding.
        if node.id in bindings:
            return bindings[node.id]
        if hasattr(builtins, node.id):
            return f"builtins.{node.id}"
        return f"{module}.{node.id}"
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


def _named_keywords(keywords: list[ast.keyword]) -> bool:
    """Whether every keyword is a named one; an unpack (``**mapping``) is not read."""
    return all(keyword.arg is not None for keyword in keywords)


def _resolves_to(
    node: ast.AST,
    aliases: dict[str, set[str]],
    module: str,
    bindings: dict[str, str] | None = None,
) -> set[str]:
    """Every symbol *node* resolves to through the module's own assignments and imports.

    The name's own binding is included beside what its aliases expand to, so a base that
    is a declared class stays resolved even when a same-named binding in the module makes
    the alias expansion take another path.
    """
    if not _plain_dotted(node):
        return set()
    symbol = _symbol(node, bindings or {}, module)
    if symbol is None:
        return set()
    return {symbol} | _canonicals(symbol, aliases)


def _readable_base(
    node: ast.AST,
    aliases: dict[str, set[str]],
    module: str,
    classes: set[str],
    bindings: dict[str, str] | None = None,
    key: str | None = None,
) -> bool:
    """Whether a class base is a plain name or a dotted attribute the scan reads.

    The base must be a plain name or a dotted attribute, and it resolves through the
    module's own assignments and imports to a class the scan inventories, to a builtin
    class, to the typing generic base, or to a symbol bound by an import into another
    module (a declaration the scan cannot see, such as a standard-library or third-party
    class). Anything else is refused: a subscript such as ``Generic[T]`` or ``BASES[0]``,
    a called base, a starred base, a constant, and a name the module binds to something
    other than one of those -- a tuple of bases, a helper, a parameter -- because each is
    a base the scan cannot follow, so the class it builds would go unread.
    """
    if not _plain_dotted(node):
        return False
    candidates = _resolves_to(node, aliases, module, bindings)
    if key:
        # The resolved names of the base's own binding, which a name bound more than once
        # holds beside the value its last assignment gave it.
        candidates |= _canonicals(key, aliases)
    if candidates & classes:
        return True
    if any(candidate.startswith("builtins.") for candidate in candidates):
        return True
    if "typing.Generic" in candidates:
        return True
    return any(candidate.split(".")[0] != module.split(".")[0] for candidate in candidates)


def _readable_decorator(node: ast.AST, aliases: dict[str, set[str]], module: str) -> bool:
    """Whether a class decorator is a form the scan fully reads.

    A decorator is read only when its resolved symbol is on the named allow-list, whether
    it is used bare or called with no positional argument and no unpack. Every other
    decorator -- one a module defines itself, one re-exported from another module under a
    new name, a call with a positional argument -- is refused, so a name this scan does not
    recognize cannot turn on a generated hash or a new base unread.
    """
    target = node.func if isinstance(node, ast.Call) else node
    if not _plain_dotted(target):
        return False
    if not (_resolves_to(target, aliases, module) & _CLASS_DECORATORS):
        return False
    if isinstance(node, ast.Call):
        return not node.args and _named_keywords(node.keywords)
    return True


def _readable_config(node: ast.AST | None, aliases: dict[str, set[str]], module: str) -> bool:
    """Whether a ``model_config`` value is a form the scan fully reads.

    A dict literal with string keys and constant values, and a call to ``dict`` or
    ``ConfigDict`` with named keyword arguments whose values are constants, are read: each
    entry is a named key the scan can look for and whose value it can see. A name, a merge,
    an unpack, a positional argument, a call to any other function or name, and any other
    expression is refused, because its entries are decided at runtime and could turn on the
    frozen configuration the scan looks for.
    """
    if isinstance(node, ast.Dict):
        return all(
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Constant)
            for key, value in zip(node.keys, node.values)
        )
    if isinstance(node, ast.Call):
        if node.args or not _named_keywords(node.keywords):
            return False
        if not (_resolves_to(node.func, aliases, module) & _CONFIG_CALLS):
            return False
        return all(isinstance(keyword.value, ast.Constant) for keyword in node.keywords)
    return False


def _config_targets(node: ast.stmt) -> list[ast.expr]:
    """The assignment targets of *node*, or an empty list for another statement kind."""
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _reads_config_class(node: ast.ClassDef) -> bool:
    """Whether a ``class Config`` body holds only plain-name constant assignments.

    Pydantic's legacy inner ``Config`` sets each option as a class attribute, so the scan
    reads the class only when every statement binds one plain name to a constant. A nested
    suite, a method, a tuple, list, starred or loop target, a walrus, a call and every other
    form is refused, because an option set there could turn on the frozen configuration
    unread. A docstring or ``pass`` carries no option and is read.
    """
    for statement in _class_body_statements(node.body, ""):
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        targets = _binding_targets(statement)
        if (
            not isinstance(statement, (ast.Assign, ast.AnnAssign))
            or len(targets) != 1
            or not isinstance(targets[0], ast.Name)
            or not isinstance(statement.value, ast.Constant)
        ):
            return False
    return True


def _binds_authority(statement: ast.stmt) -> set[str]:
    """Every authority name *statement* binds, bound as a plain name or a class.

    A binding is a plain-name assignment, annotation, augmented assignment, loop or ``with``
    target, a walrus, a ``def`` line, or a ``class`` declaration. A name that only appears
    as a value -- ``Alias = Known``, ``use(__hash__)`` -- is not a binding and is not
    reported, so the scan refuses a form that writes one of these names, not one that reads
    it.
    """
    found: set[str] = set()
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if statement.name in _AUTHORITY_NAMES:
            found.add(statement.name)
    for target in _binding_targets(statement):
        for item in ast.walk(target):
            if isinstance(item, ast.Name) and item.id in _AUTHORITY_NAMES:
                found.add(item.id)
    for item in ast.walk(statement):
        if isinstance(item, ast.NamedExpr) and isinstance(item.target, ast.Name):
            if item.target.id in _AUTHORITY_NAMES:
                found.add(item.target.id)
    return found


def _binding_targets(node: ast.stmt) -> list[ast.expr]:
    """The targets *node* binds, across the statement kinds that bind a name."""
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.target]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.optional_vars for item in node.items if item.optional_vars is not None]
    return []


def _reads_authority_binding(statement: ast.stmt, name: str) -> bool:
    """Whether *statement* binds the authority *name* in a form the scan fully reads.

    Read forms: a single plain-name ``=`` or annotated assignment for ``model_config``,
    ``__hash__`` or ``__eq__``; a ``def`` line for ``__hash__`` or ``__eq__``; and a
    ``class Config`` declaration. Every other form that binds one of those names -- a
    tuple, list, starred or loop target, a walrus, an augmented assignment, a ``with ...
    as`` target, a call, a name bound to another variable -- is not read, so the caller
    reports it rather than skipping it.
    """
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return name in {"__hash__", "__eq__"} and statement.name == name
    if name == "Config":
        return isinstance(statement, ast.ClassDef) and statement.name == "Config"
    targets = _binding_targets(statement)
    return (
        isinstance(statement, (ast.Assign, ast.AnnAssign))
        and len(targets) == 1
        and isinstance(targets[0], ast.Name)
        and targets[0].id == name
    )


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
    base_refusals: list[tuple[str, ast.AST, dict[str, str], str, str | None]] = []
    decorator_refusals: list[tuple[str, ast.AST, str]] = []
    refusals: list[tuple[str, str]] = []
    for module, (package, source) in sources.items():
        tree = ast.parse(source, filename=module)
        # The nodes a class decorator is written as, so a ``dataclass`` call written as a
        # decorator is told from one that applies the decorator to a class the scan
        # cannot follow.
        decorator_positions = {
            id(decorator)
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            for decorator in node.decorator_list
        }

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
                    if target.attr in _CLASS_AUTHORITY:
                        # Reassigning one of these rebinds what the class is or the
                        # configuration it inherits after its declaration, so a class the
                        # scan inventories could take another base, metaclass or
                        # configuration without any declaration to read.
                        refusals.append(
                            (
                                f"{module}:{target.lineno}:{target.col_offset + 1}",
                                f"an assignment to {target.attr!r} outside a class body",
                            )
                        )
                    elif target.attr in {"__hash__", "__eq__"}:
                        owner = _symbol(target.value, bindings, module)
                        if owner and not _none(value):
                            result.writes.append(
                                (owner, f"post-definition {target.attr} assignment")
                            )
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
                        # Readability is decided after every module is scanned, so a base
                        # that resolves to a class declared later still resolves. The
                        # location and the bindings are taken here, where they hold.
                        base_refusals.append(
                            (
                                f"{module}:{base.lineno}:{base.col_offset + 1}",
                                base,
                                dict(bindings),
                                module,
                                f"{scope}.{base.id}" if isinstance(base, ast.Name) else None,
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
                        # A decorator is read when it resolves to a named allow-list
                        # entry, so this too waits until every module is scanned.
                        decorator_refusals.append(
                            (
                                f"{module}:{decorator.lineno}:{decorator.col_offset + 1}",
                                decorator,
                                module,
                            )
                        )
                    for statement in _class_body_statements(node.body, module):
                        for name in sorted(_binds_authority(statement)):
                            if not _reads_authority_binding(statement, name):
                                # A form that binds an authority name but is not one the
                                # scan reads: a tuple, list, starred or loop target, a
                                # walrus, an augmented assignment, a `with ... as`
                                # target, a call, or a name bound to another variable.
                                refusals.append(
                                    (
                                        f"{module}:{statement.lineno}:{statement.col_offset + 1}",
                                        f"a binding of {name!r} the scan cannot read",
                                    )
                                )
                            elif name == "model_config" and not _readable_config(
                                statement.value,
                                result.aliases,
                                module,
                            ):
                                refusals.append(
                                    (
                                        f"{module}:{statement.lineno}:{statement.col_offset + 1}",
                                        "a model_config value the scan cannot read",
                                    )
                                )
                            elif name == "Config" and not _reads_config_class(statement):
                                refusals.append(
                                    (
                                        f"{module}:{statement.lineno}:{statement.col_offset + 1}",
                                        "a Config body the scan cannot read",
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
                elif isinstance(node, ast.AugAssign):
                    # A compound assignment adds to a name or attribute; it can rebind a
                    # class authority just as a plain assignment does, so it is read the
                    # same way.
                    assignment(node.target, node.value)
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
                        if target.attr in _CLASS_AUTHORITY | {"__hash__", "__eq__"}:
                            owner = _symbol(target.value, bindings, module)
                            if owner:
                                result.writes.append((owner, f"deleted {target.attr}"))
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
                    # ``@dataclass(...)`` is the decorator form the scan reads; the same
                    # call written elsewhere applies the decorator to a class the scan
                    # cannot follow.
                    decorated_dataclass = id(call) in decorator_positions
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
                    attribute_writer = (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr in _DELATTR_ATTRIBUTES
                    )
                    if attribute_writer and function is None:
                        # The owner of the attribute-writing name is itself a call the
                        # scan cannot follow, such as ``type(Known).__setattr__``. Its
                        # three-argument class-write form is refused; a ``super()`` call
                        # writes an instance field and is left, as is any other shape
                        # that cannot write a class attribute.
                        if len(call.args) == 3:
                            refusals.append((location, "a setattr call the scan cannot read"))
                        continue
                    if not function and not attribute_writer:
                        continue
                    owner = _symbol(call.args[0], bindings, module) if call.args else None
                    if attribute_writer and not (canonical & _SETATTR):
                        # A call through an attribute-writing name whose function the
                        # scan cannot resolve is refused: the class it writes may be one
                        # the scan inventories and the write would otherwise go unread.
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
                    if not decorated_dataclass and (canonical & {"dataclasses.dataclass"}):
                        # ``dataclass`` used as a decorator is read; used anywhere else it
                        # rewrites a class's fields and hash authority, and the scan does
                        # not follow the argument it is given, so every such call is
                        # refused.
                        refusals.append((location, "a dataclass call that is not a decorator"))

        walk(tree.body, {}, module)
    # A base is read only when its spine resolves to a class the scan inventories,
    # whichever module declares it, so this runs once every module has been scanned.
    for location, base, bindings, module, key in base_refusals:
        if not _readable_base(base, result.aliases, module, set(result.classes), bindings, key):
            refusals.append((location, "a class base the scan cannot read"))
    for location, decorator, module in decorator_refusals:
        if not _readable_decorator(decorator, result.aliases, module):
            refusals.append((location, "a decorator the scan cannot read"))
    for function, location, builds_a_class in type_calls:
        if not builds_a_class or not function:
            continue
        if "builtins.type" in _canonicals(function, result.aliases):
            refusals.append((location, "a class built with type()"))
    for function, expected, owner, description in calls:
        if _canonicals(function, result.aliases) & expected:
            result.writes.append((owner, description))
    for function, location, owner, call in setattr_calls:
        canonical = _canonicals(function, result.aliases)
        if not (canonical & _SETATTR):
            continue
        first = call.args[0] if call.args else None
        second = call.args[1] if len(call.args) >= 2 else None
        deletes = any(".__delattr__" in name for name in canonical)
        if (
            len(call.args) != 3
            or not isinstance(first, (ast.Name, ast.Attribute))
            or not isinstance(second, ast.Constant)
            or not isinstance(second.value, str)
        ):
            # A setattr or delattr whose owner or attribute the scan cannot read as a
            # literal is refused: writing a class authority through it would go unread.
            refusals.append((location, "a setattr call the scan cannot read"))
        elif second.value in _CLASS_AUTHORITY and not deletes:
            # Writing ``__bases__``, ``__class__``, ``__mro__`` or ``model_config``
            # through ``setattr`` rebinds what the class is or the configuration it
            # inherits after its declaration, so it is refused rather than reported as a
            # class write the inventory could follow.
            refusals.append((location, f"a setattr write of {second.value!r} the scan cannot read"))
        elif (
            second.value in _CLASS_AUTHORITY | {"__hash__", "__eq__"}
            and owner
            and (deletes or not _none(call.args[2]))
        ):
            result.writes.append((owner, f"{'deleted' if deletes else 'setattr'} {second.value}"))
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
    """A class body binding one of the authority names fails the gate: the reported write
    names the class, and a form the scan cannot read is refused with its location. Either
    way no body that binds ``__hash__``, ``__eq__``, ``model_config`` or ``Config`` passes
    unread.
    """

    scan = _sample("from lionagi.models import HashableModel\nclass Known(HashableModel):\n" + body)
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors
    assert any("sample.Known" in error for error in errors), errors


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
    # ``Other`` takes a base the scan cannot read: ``Alias`` holds both the family root
    # and ``object``, so the scan does not decide which one the class received and
    # reports the base. Nothing else in the module is authority of the family.
    assert _violations(scan, {*_ROOTS, "sample.Known"}) == [
        "sample:8:17: a class base the scan cannot read"
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "    __hash__, __eq__ = object.__hash__, object.__eq__",
        "    *_, __hash__ = None, None, object.__hash__",
        "    (model_config,) = ({'frozen': True},)",
        "    __hash__ += object.__hash__",
        "    with context() as __hash__:\n        pass",
        "    for __hash__ in (object.__hash__,):\n        pass",
        "    for model_config in ({'frozen': True},):\n        pass",
        "    (__hash__ := object.__hash__)",
        "    class Config:\n        (frozen,) = (True,)",
        "    class Config:\n        _ = (frozen := True)",
        "    class Config:\n        frozen: bool = _frozen()",
        "    Config = dict",
        "    model_config = CFG",
    ],
)
def test_a_class_body_authority_binding_the_scan_cannot_read_is_refused(statement):
    """Every form that binds ``__hash__``, ``__eq__``, ``model_config`` or ``Config`` in a
    class body is either read in the one form the scan understands or refused, so no
    binding of one of those names passes unread. A refused binding names its location and,
    when the binding also writes a hash, the class it writes.
    """

    scan = _sample(
        "from lionagi.models import HashableModel\nfrom pydantic import ConfigDict\n"
        "class Known(HashableModel):\n" + statement + "\n"
    )
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors, statement
    assert all(
        error.startswith("sample.Known:") or error.split(": ", 1)[0].count(":") == 2
        for error in errors
    ), errors


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
    (
        "S20",
        "BASES = (HashableModel,)\nBase = BASES[0]\nclass K(Base):\n"
        "    __hash__ = object.__hash__\nclass Known(HashableModel):\n    pass\n",
    ),
    (
        "S21",
        "CFG = {'frozen': True}\nclass Known(HashableModel):\n    model_config = CFG.copy()\n",
    ),
    (
        "S22",
        "def make_cfg():\n    return {}\nclass Known(HashableModel):\n"
        "    model_config = make_cfg()\n",
    ),
    (
        "S23",
        "class Known(HashableModel):\n    class Config:\n        (frozen,) = (True,)\n",
    ),
    (
        "S24",
        "class Known(HashableModel):\n    class Config:\n        _ = (frozen := True)\n",
    ),
    (
        "S25",
        "class Known(HashableModel):\n    *_, __hash__ = None, None, object.__hash__\n",
    ),
    (
        "S26",
        "class Known(HashableModel):\n    for __hash__ in (object.__hash__,):\n        pass\n",
    ),
    (
        "S27",
        "class Known(HashableModel):\n    for model_config in ({'frozen': True},):\n        pass\n",
    ),
    (
        "S28",
        "class Mixin: pass\nclass Known(HashableModel):\n    pass\nKnown.__bases__ += (Mixin,)\n",
    ),
    (
        "S29",
        "class Plain:\n    __hash__ = object.__hash__\n"
        "setattr(Plain, '__bases__', (HashableModel,))\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "S30",
        "class Known(HashableModel):\n    pass\ndataclass(unsafe_hash=True)(Known)\n",
    ),
    (
        "S31",
        "class Known(HashableModel):\n    pass\ndataclass(cls=Known, unsafe_hash=True)\n",
    ),
    (
        "S32",
        "FROZEN = {'unsafe_hash': True}\nclass Known(HashableModel):\n    pass\n"
        "dataclass(Known, **FROZEN)\n",
    ),
    (
        "S33",
        "class Known(HashableModel):\n    pass\ndataclass([Known][0], unsafe_hash=True)\n",
    ),
    (
        "S34",
        "class Known(HashableModel):\n    pass\ndelattr(Known, '__hash__')\n",
    ),
    (
        "S35",
        "class Known(HashableModel):\n    pass\ntype.__delattr__(Known, '__hash__')\n",
    ),
    (
        "S36",
        "ATTR = '__hash__'\nclass Known(HashableModel):\n    pass\n"
        "type(Known).__setattr__(Known, ATTR, object.__hash__)\n",
    ),
    (
        "S37",
        "from helpers import add_hash\n@add_hash\nclass Known(HashableModel):\n    pass\n",
    ),
]
# A helper module the shapes that follow a re-export need, so a re-exported name is
# resolved after every module is scanned.
_UNREADABLE_SHAPE_MODULES = {
    "S18": {"helpers": ("", "from builtins import type as maker\n")},
    "S37": {"helpers": ("", "def add_hash(cls):\n    return cls\n")},
}


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


_TYPE_STATEMENT_CASES = {"C22", "C23"}


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param(
            snippet,
            marks=pytest.mark.skipif(
                case_id in _TYPE_STATEMENT_CASES and sys.version_info < (3, 12),
                reason="type statements are 3.12 syntax",
            ),
            id=case_id,
        )
        for case_id, snippet in _SCAN_REFUSAL_CASES
    ],
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
