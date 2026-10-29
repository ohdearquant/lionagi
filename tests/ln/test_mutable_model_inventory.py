"""Closed mutable-model declarations and statically resolvable hash authority.

The scan follows imports, reexports, lexical name/attribute aliases (assignments,
assignment expressions, loop and ``with`` targets, and loop targets over a literal tuple,
list or set) and declared inheritance. A name a nested suite binds keeps the value it held
before the suite among them: an enclosing scope's binding, a function parameter, or the
module global or builtin it names, so a branch that may not run cannot replace it. A
declaration made in alternative suites counts as one binding on either path, while a name
one path binds twice keeps that path's count. A dotted name resolves through every value
its root was given, including a class attribute assigned after the class is declared and an
item written through ``vars(X)`` or ``X.__dict__``. Calls are read in every expression a
statement evaluates, including tests, context managers, loop iterables, return values,
assertions and match subjects and guards. Every statement body is walked, a ``match``
statement's case clauses included, and a statement kind the walk cannot classify is
reported rather than skipped.

Every binding of a name is recorded per scope: an assignment of any target shape, an
augmented or annotated assignment, an ``import`` or ``from`` alias, a ``class``, a
``def``, a ``for`` or ``with`` target, an ``except`` target, a walrus, a ``global`` or
``nonlocal`` declaration, and a ``del``. So is every write or delete of an attribute or
subscript whose root name the scan reads, ``setattr`` and ``delattr`` included. After the
walk, a name the scan relies on -- a class base, a decorator, a ``setattr`` target,
``super``, or the callee or argument of a hash call -- resolves only when it is bound
exactly once in the innermost scope Python makes visible at that use, by an ``import``, a
``class`` or a plain-name assignment from a value the scan reads exactly, and that binding
precedes the use. Everything else is refused. A class body is not visible from a method or a
nested class, so a base or decorator of a class nested in a class body is refused rather
than resolved through the enclosing class's names.

The scan is fail-closed, and the property that makes the rule checkable is this. A form
whose meaning the scan cannot resolve to a single value is refused as "cannot read" rather
than guessed at, and a refusal is never resolved by choosing one of the values a name might
hold. A rule that protects the property is observable when it is undone: a sample that
defines or reaches a hashable mutable descendant -- a class descending from
``HashableModel`` or ``DataClass`` -- or one that writes ``__hash__`` or a frozen
configuration onto a family class, scans with no violation and no refusal. The tests below
reproduce each such sample, watch it scan clean with the rule removed, and confirm that the
rule refuses it. Refusing a form the framework's own tree never writes is acceptable; what
is not acceptable is a form the tree does write that the scan cannot read.

The shapes the scan reads are a class base that is a plain name or a dotted attribute
whose resolved symbol is a declared class, a builtin class, an imported symbol or
``Generic``; class keywords, decorator keywords and ``model_config`` entries written as
named keyword arguments; a ``model_config`` value given as a dict literal with string keys
and constant values, or as a call to ``ConfigDict`` or ``dict`` whose own arguments are
named keyword arguments with constant values; and a ``model_config`` assigned to a single
plain name. A decorator is read when its resolved symbol is one of the audited
names on the allow-list, once every module's aliases have resolved. A class body binds
``__hash__``, ``__eq__``, ``model_config`` or ``Config`` only in a form the scan fully
reads, and every other binding of one of those names is refused. A class name declared
twice in one scope is refused, because only the last declaration would remain. A base, a
decorator, a ``setattr`` target or a class body's write target is read only when the name
it uses was bound once, before its use, in the scope it resolves in, and resolves to a
single readable value; a name bound twice, bound after its use, taken from a subscript,
given a value the scan does not read, or declared ``global`` or ``nonlocal`` in a function
is refused rather than resolved, because which binding applies is decided by runtime order
the scan does not model. ``super`` is read as a writer only through the bare builtin form,
and only when the name resolves to nothing but ``builtins.super``. A call that carries a
hash keyword and passes a class the scan inventories is refused, because the scan does not
follow what the call does with the class; so is a hash call whose callee or arguments
involve ``*args``, ``**kwargs``, currying through ``functools.partial``, a lambda or a
subscript. A ``Field`` declaration's ``frozen`` keyword freezes that field, not the class,
and is not read as a hash call. Any write or delete of ``__hash__``, ``__eq__`` or a
frozen or configuration setting on a family class or a descendant -- by attribute
assignment, ``del``, ``setattr``, ``delattr``, or a write through ``vars`` or
``__dict__`` -- is a violation when the target resolves, and a refusal when it does not.
Everything else is a "cannot read" violation,
naming the file and line: a statement kind the walk does not know, a class base, class
keyword or decorator outside those forms, an attribute assignment whose owner is not a
plain name, an assignment to
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
# The value of a function parameter, which is whatever the caller passes.
_PARAMETER = "<parameter>"
_SETATTR = frozenset(
    {
        "builtins.setattr",
        "builtins.delattr",
        "builtins.type.__setattr__",
        "builtins.type.__delattr__",
        "builtins.object.__setattr__",
        "builtins.object.__delattr__",
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
# A call that declares one model field. Its ``frozen`` keyword freezes the field, not the
# class, so it is not a class hash call even when its other arguments are values the scan
# does not follow.
_FIELD_CALLS = frozenset({"pydantic.Field"})
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
    "lionagi._auto:315:9": "a marker written onto a handler function, not a model",
    "lionagi.ln.types.base:224:13": "a declared field set from its layout during init",
    "lionagi.ln.types.base:367:13": "a declared field copied by name during update",
    "lionagi.models.operable_model:132:13": "an extra field set by name",
    "lionagi.models.operable_model:142:17": "an extra field reset to its default",
    "lionagi.models.operable_model:145:17": "an extra field reset from its factory",
    "lionagi.models.operable_model:255:9": "an extra field set by name",
    "lionagi.models.operable_model:275:13": "a field attribute set by name on a field object",
    "lionagi.protocols.generic.pile:539:13": "a field attribute copied by name while copying",
    "lionagi.protocols.messages.instruction:101:17": "a declared field set by name",
    "lionagi.protocols.messages.instruction:147:17": "a declared field set by name",
    "lionagi.protocols.messages.message:181:9": "a declared field set by name",
    "lionagi.protocols.messages.message:193:13": "a render-input field set by name",
    "lionagi.providers.anthropic.claude_code:1143:17": "a runtime field copied by name",
    "lionagi.service.connections.endpoint_config:258:17": "a config key set by name",
    "lionagi.service.connections.mcp_wrapper:86:9": "a probe attribute set on an exception",
    "lionagi.state.compat:1702:13": (
        "a frozen dataclass folding its own string fields in __post_init__"
    ),
    # A class base the scan cannot read: a base subscripted by a type parameter. Each
    # declares no hash authority of its own; the refused node is the subscript.
    "lionagi.adapters._base:305:15": "``Protocol[T]``, a protocol parameterized by a type",
    "lionagi.adapters._base:482:20": "``Protocol[T]``, a protocol parameterized by a type",
    "lionagi.adapters.async_postgres_adapter:21:39": "``AsyncPostgresAdapter[T]``, an imported adapter",
    "lionagi.adapters.csv_:26:31": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.adapters.json_:26:32": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.adapters.pandas_:37:37": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.adapters.toml_:33:32": "``Adapter[T]``, the runtime-checkable protocol base",
    "lionagi.ln._cache:19:23": "``Generic[K, V]``, the typing generic base",
    "lionagi.ln.concurrency.primitives:126:13": "``Generic[T]``, the typing generic base",
    "lionagi.ln.types.registry:95:29": "``Generic[ItemT]``, the typing generic base",
    "lionagi.ln.types.registry:108:32": "``Generic[ItemT]``, the typing generic base",
    "lionagi.ln.types.registry:193:30": "``Generic[ItemT]``, the typing generic base",
    "lionagi.ln.types.registry:208:32": "``Generic[ItemT]``, the typing generic base",
    "lionagi.ln.types.registry:381:24": "``Generic[ItemT]``, the typing generic base",
    "lionagi.protocols._concepts:110:21": "``Generic[E]``, the typing generic base",
    "lionagi.protocols._concepts:98:23": "``Generic[E]``, the typing generic base",
    "lionagi.protocols.generic.element:301:10": "``Generic[E]``, the typing generic base",
    "lionagi.protocols.generic.flow:25:21": "``Generic[E, P]``, the typing generic base",
    "lionagi.protocols.generic.pile:187:21": "``Collective[T]``, the collection base on ``Element``",
    "lionagi.protocols.generic.pile:187:36": "``Generic[T]``, the typing generic base",
    "lionagi.protocols.generic.progression:139:28": "``Ordering[T]``, the ordering base on ``Element``",
    "lionagi.protocols.generic.progression:139:41": "``Generic[T]``, the typing generic base",
    "lionagi.protocols.graph.graph:33:34": "``Generic[T]``, the typing generic base",
    # A class base whose module is imported inside the function that defines the class,
    # so the scan cannot resolve the name; the class is an exception, not a model:
    "lionagi.service.connections.endpoint:97:36": "``httpx.ConnectError``, imported lazily",
    # A base reached through a submodule import (``import importlib.abc``), which the scan
    # does not resolve; each class is an import hook, not a model:
    "lionagi._moved:106:20": "``importlib.abc.Loader``, the import system's loader base",
    "lionagi._moved:138:20": "``importlib.abc.MetaPathFinder``, the import system's finder base",
    # A metaclass the scan does not inventory; the class declares no hash authority
    # and is outside the mutable family:
    "lionagi.ln.types._sentinel:38:1": "a singleton metaclass, not a mutable model",
    # An attribute assignment whose owner is an item of a list, not a name or a
    # dotted attribute; the scan refuses that owner shape, and here it writes an
    # edge's label on an already-built edge:
    "lionagi.operations.flow:1546:21": "an edge label on a list item, not a class attribute",
    # A class built at runtime by a factory from a parameter; it is outside what the
    # scan proves, and it declares no hash authority:
    "lionagi.protocols.graph.node_factory:144:11": "a node class built by a factory",
    # An assignment to a class authority outside a class body; here it rebinds one
    # connection object's class to a stop-once subclass, on an object the scan does
    # not inventory:
    "lionagi.state.engine:244:9": "a connection object taking a runtime subclass",
    # A class whose base is a function parameter: the scan cannot see the class the
    # parameter holds, and each declares no hash authority of its own:
    "lionagi.state.engine:182:30": "a subclass of a caller-supplied connection class",
    "lionagi.testing.helpers:228:22": "a subclass of the class a classmethod is called on",
    # A ``model_config`` the scan cannot read in full: ``SettingsConfigDict`` is the
    # settings analogue of ``ConfigDict``, and the entry below passes a callable rather
    # than a constant. Neither sets a frozen configuration, so neither class hashes:
    "lionagi.config:171:5": "a pydantic settings config, which sets no frozen option",
    "lionagi.studio.operator.types:52:5": "a config whose alias generator is a callable",
    # An attribute assignment whose owner is not a plain name; here each increments a
    # counter field on an object held in a dict slot:
    "lionagi.studio.scheduler.signals:218:17": "a counter field on a dict-held object",
    "lionagi.service.accounts._router:608:13": (
        "a counter on a held character popped from a list, not a class attribute"
    ),
    "lionagi.studio.scheduler.signals:221:17": "a counter field on a dict-held object",
}


@dataclass
class _Class:
    node: ast.ClassDef
    location: str
    bases: tuple[str, ...]
    decorators: tuple[tuple[str, ast.expr], ...]


@dataclass
class _Mutation:
    """A write the walk reads, resolved once every module's bindings are known."""

    location: str
    kind: str
    module: str
    owner: ast.AST
    bindings: dict[str, str]
    before: dict[str, frozenset[str]] | None
    scope: str
    attribute: str | None
    operation: str | None
    order: int
    value: ast.AST | None = None
    gated: bool = True


@dataclass
class _Scan:
    classes: dict[str, _Class]
    aliases: dict[str, set[str]]
    symbols: set[str]
    writes: list[tuple[str, str]]
    refusals: list[tuple[str, str]]
    # How many times each name was given a new value, by the alias key of its scope -- an
    # assignment, a walrus or a loop target. A name given a second value holds more than
    # one, and which a reference receives is decided by runtime order the scan does not
    # model, so it is refused where a base, decorator or target needs it resolved. A
    # declared class or an import binds the name once and is not counted.
    binds: dict[str, int] | None = None
    # The ``<scope>.<name>`` keys a function declared ``global`` or ``nonlocal``. Such a
    # binding writes a name outside the scope the scan walks under, so the scan cannot
    # follow it and refuses a base, decorator or target it supplies.
    declared: set[str] | None = None
    # The scopes that are class bodies. Python does not make a class scope visible to a
    # method or a nested class body, so a reference there resolves only through its own
    # scope and the scopes enclosing the class. A class base or decorator expression is
    # evaluated in the scope that holds the class statement, which the walk records
    # directly, so this set excludes only scopes a nested use cannot read.
    class_scopes: set[str] | None = None
    # Every write or delete of a name, by the module and the alias key its scope records.
    # A write form the walk reads -- an attribute target, a ``setattr`` call, a dictionary
    # write through ``vars`` or ``__dict__`` -- records the attribute and the owner
    # expression here. The reference is resolved after the walk, so a name bound twice is
    # refused rather than read against whichever binding happened to be current.
    mutations: list[_Mutation] | None = None
    # The ``<scope>.<name>`` keys a declaration binds -- an ``import``, a ``class`` or a
    # ``def``. A declaration and a later value are two bindings of one name, so the count a
    # reference is checked against adds the declarations to the new values.
    declarations: set[str] | None = None
    # How many declarations bound each key. A name imported twice, or imported and then
    # assigned, holds more than one value, so a reference that needs the name resolved is
    # refused rather than read against whichever declaration happened to be current.
    declaration_counts: dict[str, int] | None = None
    # The walk's position, and the position of each alias value's first binding. A
    # reference reads a value only when that value was bound before the reference.
    order: int = 0
    alias_first: dict[tuple[str, str], int] | None = None


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


def _alias_key(node: ast.AST, scope: str) -> str | None:
    """The alias key a name's accumulated bindings are recorded under, or ``None``.

    A dotted attribute chain is keyed by its root name, because that is the name the
    module's own bindings record; a call is keyed by its function, so a ``model_config``
    value written as ``make_cfg(...)`` finds the bindings of ``make_cfg``. A subscript is
    keyed by nothing.
    """
    if isinstance(node, ast.Call):
        node = node.func
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name):
        return f"{scope}.{root.id}"
    return None


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


def _name_candidates(
    node: ast.AST,
    aliases: dict[str, set[str]],
    module: str,
    symbols: set[str],
    bindings: dict[str, str] | None = None,
    key: str | None = None,
) -> set[str] | None:
    """Every value the name *node* was bound to, or ``None`` for another node shape.

    A plain name resolves to every value it was given in its scope -- the value a second
    assignment, or a binding in a nested suite, gave it beside the first -- so a name bound
    twice is read only when each of those values is readable. A value the scan itself
    declared -- a class or a function in one of the scanned modules -- is that declaration
    and is judged as it stands. Any other value is expanded through the module's aliases,
    so a re-export such as ``models.HashableModel`` resolves to the class it names. A
    dotted attribute chain is expanded through every value its root name was given, so
    a root bound to two modules names the attribute of each.
    """
    if not _plain_dotted(node):
        return None
    symbol = _symbol(node, bindings or {}, module)
    if symbol is None:
        return None
    if key and key in aliases:
        # A dotted chain is keyed by its root name, so each value the root was given
        # carries the rest of the chain.
        rest: list[str] = []
        root = node
        while isinstance(root, ast.Attribute):
            rest.insert(0, root.attr)
            root = root.value
        values = {".".join([value, *rest]) for value in aliases[key]}
    else:
        values = {symbol}
    candidates: set[str] = set()
    for value in values:
        if value in symbols:
            candidates.add(value)
        else:
            candidates |= _canonicals(value, aliases)
    return candidates


def _visible_candidates(
    node: ast.AST,
    aliases: dict[str, set[str]],
    module: str,
    bindings: dict[str, str],
    scope: str,
) -> set[str] | None:
    """Every value a bare name can hold in *scope*, through every binding it can see.

    A name resolves in the innermost scope that binds it, but a second binding of the same
    spelling in an enclosing scope is still a value some references receive -- a module
    rebinding seen from inside a function that does not rebind it, most often -- so every
    value the name can hold is returned rather than the single one the innermost binding
    names. A name no scope here binds resolves to the builtin it names.
    """
    keys = _binding_keys(node, scope)
    if keys is None:
        return None
    candidates: set[str] = set()
    for key in keys:
        for value in aliases.get(key, set()):
            candidates |= _canonicals(value, aliases)
    if not candidates:
        symbol = _symbol(node, bindings, module)
        if symbol is not None:
            candidates |= _canonicals(symbol, aliases)
    return candidates


def _readable_symbol(candidate: str, module: str, classes: set[str]) -> bool:
    """Whether one resolved symbol is a base, decorator or config the scan can follow.

    A symbol is readable when it names a class the scan inventories, a builtin class, the
    typing generic base, or a symbol bound into another module (a declaration the scan
    cannot see, such as a standard-library or third-party class). Every other symbol -- a
    tuple of bases, a helper, a parameter -- is a value the scan cannot follow.
    """
    if candidate in (_UNRESOLVED, _PARAMETER):
        return False
    if candidate in classes:
        return True
    if candidate.startswith("builtins."):
        return True
    if candidate == "typing.Generic":
        return True
    return candidate.split(".")[0] != module.split(".")[0]


def _readable_base(
    node: ast.AST,
    aliases: dict[str, set[str]],
    module: str,
    classes: set[str],
    symbols: set[str],
    bindings: dict[str, str] | None = None,
    key: str | None = None,
    owner: str | None = None,
    before: dict[str, frozenset[str]] | None = None,
    scope: str = "",
    counts: dict[str, int] | None = None,
    class_scopes: set[str] | None = None,
    declarations: set[str] | None = None,
    declaration_counts: dict[str, int] | None = None,
    alias_first: dict[tuple[str, str], int] | None = None,
    evaluation: str | None = None,
) -> bool:
    """Whether a class base is a plain name or a dotted attribute the scan reads.

    The base must be a plain name or a dotted attribute, and EVERY value its name was
    bound to -- the value the name holds where the class is declared, and every value it
    was given in an enclosing suite -- must resolve to a class the scan inventories, to a
    builtin class, to the typing generic base, or to a symbol bound by an import into
    another module (a declaration the scan cannot see, such as a standard-library or
    third-party class). Anything else is refused: a subscript such as ``Generic[T]`` or
    ``BASES[0]``, a called base, a starred base, a constant, and a name any of whose
    bindings resolves to something other than one of those -- a tuple of bases, a helper,
    a parameter -- because the scan cannot decide which value the class received, so the
    class it builds would go unread. A name with one unreadable binding is refused even
    when another binding is a declared class.
    """
    if owner and not _read_once(
        node,
        aliases,
        bindings or {},
        before,
        scope,
        counts,
        class_scopes,
        declarations,
        declaration_counts,
        alias_first,
        evaluation,
    ):
        # The name was bound more than once in an enclosing scope, bound after this class
        # was declared, or declared ``global``/``nonlocal``; the scan does not resolve
        # which value the class received, so the base is refused rather than guessed.
        return False
    candidates = _name_candidates(node, aliases, module, symbols, bindings, key)
    return bool(candidates) and all(
        _readable_symbol(candidate, module, classes) for candidate in candidates
    )


def _binding_keys(node: ast.AST, scope: str) -> set[str] | None:
    """The alias keys a reference to *node* can see from *scope*, or ``None``.

    A name resolves in the scope a reference sits in and in each scope enclosing it, and a
    binding in any other scope -- a sibling function's local, another module's global -- is
    not a value this reference can reach. The keys are therefore the name bound in *scope*
    and in each scope enclosing it, out to the module: what the caller reads is only what
    the reference could see.
    """
    if not _plain_dotted(node):
        return None
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name):
        return None
    keys = {f"{root.id}"}
    parts = scope.split(".")
    while parts:
        keys.add(".".join([*parts, root.id]))
        parts.pop()
    return keys


def _bindings_of(
    node: ast.AST,
    aliases: dict[str, set[str]],
    scope: str,
    bindings: dict[str, str] | None = None,
    class_scopes: set[str] | None = None,
    evaluation: str | None = None,
) -> dict[str, frozenset[str]] | None:
    """The values the reference *node* sees, keyed by the binding it would read.

    The values are taken when the reference is read, so the caller can tell a binding made
    after the reference from one that was already there.
    """
    keys = _visible_keys(node, bindings or {}, scope, class_scopes, evaluation)
    if keys is None:
        return None
    return {key: frozenset(aliases[key]) for key in keys if key in aliases}


def _binding_count(
    node: ast.AST,
    bindings: dict[str, str],
    counts: dict[str, int] | None,
    scope: str,
    class_scopes: set[str] | None = None,
    declarations: set[str] | None = None,
    declaration_counts: dict[str, int] | None = None,
    alias_first: dict[tuple[str, str], int] | None = None,
    evaluation: str | None = None,
) -> int:
    """How many times the name *node* roots at was bound in the scope it resolves in.

    A name resolves in the innermost scope that binds it -- a function local before the
    module global of the same spelling -- so the count is taken for that scope alone. A
    name bound more than once there -- by a second assignment, by a fallback in an
    ``except`` clause or an ``if`` arm, by a rebinding after its use -- holds more than one
    value, and which value a reference receives is decided by runtime order the scan does
    not model. The count is what the scan refuses on rather than the values, so a name it
    cannot resolve is never guessed at.
    """
    if counts is None or _binding_keys(node, scope) is None:
        return 0
    for key in _visible_keys(node, bindings, scope, class_scopes, evaluation):
        total = counts.get(key, 0)
        if declaration_counts is not None:
            total += declaration_counts.get(key, 0)
        elif declarations and key in declarations:
            total += 1
        if total:
            return total
    return 0


def _innermost_first(node: ast.AST, scope: str) -> list[str]:
    """The alias keys *node* can resolve through, innermost scope first.

    The root name bound in the reference's own scope comes first, then the same name in
    each scope enclosing it out to the module, which is the order Python looks them up in.
    """
    if not _plain_dotted(node):
        return []
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name):
        return []
    keys = [root.id]
    parts = scope.split(".")
    while parts:
        keys.append(".".join([*parts, root.id]))
        parts.pop()
    return keys


def _visible_keys(
    node: ast.AST,
    bindings: dict[str, str],
    scope: str,
    class_scopes: set[str] | None = None,
    evaluation: str | None = None,
) -> list[str]:
    """The alias keys a nested use of *node* can resolve through, innermost first.

    Python's own rules decide which scopes a reference sees. A reference in a function,
    a method or a nested class body sees the class body only when the class body is that
    reference's own scope; a class scope is never visible from a nested scope. So a key
    for an enclosing class body is skipped, while a key for a module or function scope is
    kept. The ``scope`` itself is always kept, because a reference resolves its own
    scope before any scope enclosing it.
    """
    read_scope = evaluation or scope
    keys = _innermost_first(node, read_scope)
    if not keys or not class_scopes:
        return keys
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name):
        return keys
    if (
        f"{read_scope}.{root.id}" in bindings
        or read_scope == root.id
        or f"{scope}.{root.id}" in bindings
    ):
        return keys
    visible = [keys[0]]
    for key in keys[1:]:
        prefix = key[: -len(key.rsplit(".", 1)[-1]) - 1] if "." in key else ""
        if prefix and (prefix in class_scopes or prefix.startswith(f"{read_scope}.<locals>")):
            # A class body is invisible to a nested function or class, and so is the body
            # of the function that encloses one: the class is declared when the function
            # runs, so a name the class body binds cannot supply its own base either.
            continue
        if key in bindings:
            break
        visible.append(key)
    return visible


def _read_once(
    node: ast.AST,
    aliases: dict[str, set[str]],
    bindings: dict[str, str],
    before: dict[str, frozenset[str]] | None,
    scope: str,
    counts: dict[str, int] | None = None,
    class_scopes: set[str] | None = None,
    declarations: set[str] | None = None,
    declaration_counts: dict[str, int] | None = None,
    alias_first: dict[tuple[str, str], int] | None = None,
    evaluation: str | None = None,
    reference_order: int | None = None,
) -> bool:
    """Whether a reference's name is bound exactly once, and bound before the reference.

    The scan reads a base, decorator, ``setattr`` target or write owner only when the name
    it names was bound once, before the class or call that uses it. A name bound more than once, bound
    after its use, or declared ``global``/``nonlocal`` -- either of which moves the binding
    out of the scope the scan walks -- is a value the scan cannot resolve, because which
    binding applies is decided by the runtime order the scan does not model. Such a name is
    refused rather than read. A name nothing here binds -- a builtin, or a symbol from an
    unread module -- has no binding to count and is left to the caller's other rules.
    """
    if before is None or not _readable_value(
        node, bindings, before, scope, class_scopes, evaluation
    ):
        return False
    if counts is None:
        return True
    total = _binding_count(
        node,
        bindings,
        counts,
        scope,
        class_scopes,
        declarations,
        declaration_counts,
        alias_first,
        evaluation,
    )
    if total > 1 or (not before and total):
        # A second binding, or a binding made after the reference, leaves the scan
        # unable to tell which value the reference receives. A name the reference read
        # as the builtin because no binding existed yet is refused once a later binding
        # appears.
        # The name was bound more than once -- by a second assignment, by a fallback in an
        # ``except`` clause or an ``if`` arm -- so it holds more than one value and the
        # scan cannot tell which one the reference receives. It is refused rather than
        # resolved, which is what "read only the shapes it fully understands" requires.
        return False
    if reference_order is not None and alias_first is not None:
        for key in _visible_keys(node, bindings, scope, class_scopes, evaluation):
            if any(
                bound == key and first >= reference_order
                for (bound, _), first in alias_first.items()
            ):
                # The value the name holds here was bound at or after the reference.
                return False
    return True


def _readable_value(
    node: ast.AST,
    bindings: dict[str, str],
    before: dict[str, frozenset[str]] | None,
    scope: str,
    class_scopes: set[str] | None = None,
    evaluation: str | None = None,
) -> bool:
    """Whether every value the reference could read now was bound before its use.

    A binding that appeared after the reference was taken is not a value the reference
    receives, so it does not make the reference unreadable; a reference still needs the
    binding it saw before its use. The innermost scope that binds the name is the one
    Python reads, and only its values decide the reference. A name whose visible
    binding is outside the class scope a nested use cannot see is refused.
    """
    if before is None:
        return False
    keys = _visible_keys(node, bindings, scope, class_scopes, evaluation)
    # The values seen at the reference are what must still be present in the innermost
    # binding Python reads. A superset adds a later binding, which this reference never
    # receives; a different set means the binding it saw was replaced, which the count
    # refuses. A key outside the scopes a nested use can read is refused by its absence.
    if not before:
        # No scope the reference can see binds the name, so it reads a builtin or a
        # symbol from an unread module. A name bound in a visible scope but absent from
        # ``bindings`` was bound after the reference, which the binding count refuses.
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        return (
            isinstance(root, ast.Name)
            and not any(key in bindings for key in keys)
            and hasattr(builtins, root.id)
        )
    if not all(before.values()):
        return False
    # A name the reference reads a scope for that had no binding is a builtin or a symbol
    # from an unread module; a name with a binding in a visible scope must have been
    # bound before the reference. The innermost visible binding is the one Python reads.
    return any(key in before for key in keys) or len(keys) > 1


def _hash_keywords_of(
    func: ast.AST,
    module: str,
    bindings: dict[str, str],
    aliases: dict[str, set[str]],
) -> list[ast.keyword]:
    """The hash keywords a curried call on *func* carries, if *func* is a ``partial``.

    A ``functools.partial`` stores the keywords it will pass, so
    ``partial(decorate, unsafe_hash=True)(Known)`` applies a hash-keyword call the scan
    does not follow. Every other callee carries none here.
    """
    if not (
        isinstance(func, ast.Call)
        and func.keywords
        and isinstance(func.func, (ast.Name, ast.Attribute))
        and "functools.partial" in _canonicals(_symbol(func.func, bindings, module) or "", aliases)
    ):
        return []
    return list(func.keywords)


def _bindings_before(
    scan: _Scan,
    symbol: str,
    bindings: dict[str, str],
    before: dict[str, frozenset[str]] | None,
    scope: str,
    order: int | None = None,
) -> bool:
    """Whether a recorded argument's symbol was bound once, before the call.

    The argument is recorded as its symbol rather than as a node, so the check is made
    with the alias key the argument's root name records, which is the symbol's last
    segment. A builtin or an unread module's symbol has no binding to count; a symbol
    bound in a visible scope must have been bound once, before the call.
    """
    root = symbol.rsplit(".", 1)[-1]
    key = f"{scope}.{root}"
    if not before:
        # The argument's root name is not one this scope binds: a module path or a
        # builtin, which the read itself leaves to the caller's other rules.
        if symbol in scan.symbols or (
            key not in bindings
            and root not in bindings
            and not any(bound.endswith(f".{root}") for bound in scan.binds or ())
        ):
            return True
    if key not in bindings and root not in bindings:
        # The argument's root name is not one this scope binds: a module path or a
        # builtin, which the read itself leaves to the caller's other rules.
        return True
    if symbol in scan.symbols:
        # A class or function this scan declared: the call names the declaration itself.
        return True
    return _read_once(
        ast.Name(id=root),
        scan.aliases,
        bindings,
        before,
        scope,
        scan.binds,
        scan.class_scopes,
        scan.declarations,
        scan.declaration_counts,
        scan.alias_first,
        reference_order=order,
    )


def _readable_decorator(
    node: ast.AST,
    aliases: dict[str, set[str]],
    module: str,
    symbols: set[str],
    bindings: dict[str, str] | None = None,
    key: str | None = None,
    owner: str | None = None,
    before: dict[str, frozenset[str]] | None = None,
    scope: str = "",
    counts: dict[str, int] | None = None,
    class_scopes: set[str] | None = None,
    declarations: set[str] | None = None,
    declaration_counts: dict[str, int] | None = None,
    alias_first: dict[tuple[str, str], int] | None = None,
    evaluation: str | None = None,
) -> bool:
    """Whether a class decorator is a form the scan fully reads.

    A decorator is read only when its name is bound exactly once where the class is
    declared -- a name bound more than once, bound after its use, or declared
    ``global``/``nonlocal`` is refused -- and EVERY value that name was bound to resolves
    to a name on the allow-list, used bare or called with no positional argument and no
    unpack. Every other decorator -- one a module defines itself, one re-exported from
    another module under a new name, a name that also holds an unrecognized value, a call
    with a positional argument -- is refused, so a name this scan does not recognize cannot
    turn on a generated hash or a new base unread.
    """
    target = node.func if isinstance(node, ast.Call) else node
    if owner and not _read_once(
        target,
        aliases,
        bindings or {},
        before,
        scope,
        counts,
        class_scopes,
        declarations,
        declaration_counts,
        alias_first,
        evaluation,
    ):
        return False
    candidates = _name_candidates(target, aliases, module, symbols, bindings=bindings, key=key)
    if not candidates or not candidates <= _CLASS_DECORATORS:
        return False
    if isinstance(node, ast.Call):
        return not node.args and _named_keywords(node.keywords)
    return True


def _readable_config(
    node: ast.AST | None,
    aliases: dict[str, set[str]],
    module: str,
    symbols: set[str],
    bindings: dict[str, str] | None = None,
    key: str | None = None,
) -> bool:
    """Whether a ``model_config`` value is a form the scan fully reads.

    A dict literal with string keys and constant values, and a call to ``dict`` or
    ``ConfigDict`` with named keyword arguments whose values are constants, are read: each
    entry is a named key the scan can look for and whose value it can see. A name, a merge,
    an unpack, a positional argument, a call to any other function or name, a name that
    also holds another value, and any other expression is refused, because its entries are
    decided at runtime and could turn on the frozen configuration the scan looks for.
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
        candidates = _name_candidates(
            node.func, aliases, module, symbols, bindings=bindings, key=key
        )
        if not candidates or not candidates <= _CONFIG_CALLS:
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
    unread. A docstring or ``pass`` carries no option and is read. A base, a class keyword
    or a decorator is refused too: each could supply an option -- a frozen setting among
    them -- that the class body does not set.
    """
    if node.bases or node.keywords or node.decorator_list:
        return False
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


def _class_dictionary_owner(node: ast.AST) -> ast.AST | None:
    """The class expression whose ``__dict__`` or ``vars()`` *node* names, if any.

    ``vars(X)`` and ``X.__dict__`` both return the class dictionary, so a subscript
    assignment or a method call on either one can write an attribute of ``X``. Any other
    subscript or attribute is not a class dictionary the scan can name.
    """
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "vars"
            and len(node.args) == 1
            and not node.keywords
        ):
            return node.args[0]
        return None
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return node.value
    return None


_DICTIONARY_WRITERS = frozenset({"update", "setdefault", "__setitem__", "clear", "pop", "popitem"})


def _literal_names(value: ast.AST) -> set[str] | None:
    """The literal string keys a mapping value names, or ``None`` if any key is unread.

    A dict literal with string-constant keys and a ``dict`` call with named keyword keys
    have keys the scan can read. Anything else -- an unpack, a computed key, a name --
    has a key set the scan does not know.
    """
    if isinstance(value, ast.Dict):
        keys = set()
        for key in value.keys:
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            keys.add(key.value)
        return keys
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"dict", "vars"}
        and not value.args
    ):
        names = set()
        for keyword in value.keywords:
            if keyword.arg is None:
                return None
            names.add(keyword.arg)
        return names
    return None


def _dictionary_write(target: ast.Subscript) -> tuple[ast.AST, set[str] | None] | None:
    """The class owner and literal item names a subscript target writes, if it writes one.

    A subscript of ``vars(X)`` or ``X.__dict__`` writes one item. A call on such a
    dictionary -- ``update``, ``setdefault``, ``__setitem__`` -- writes the names its
    argument names. Any other subscript is not a class dictionary.
    """
    owner = _class_dictionary_owner(target.value)
    if owner is not None:
        item = target.slice
        name = item.value if isinstance(item, ast.Constant) else None
        return (owner, {name} if isinstance(name, str) else None)
    receiver = target.value
    for method in _DICTIONARY_WRITERS:
        if isinstance(receiver, ast.Attribute) and receiver.attr == method:
            owner = _class_dictionary_owner(receiver.value)
            if owner is None:
                continue
            call = target.slice
            if isinstance(call, ast.Call) and not call.keywords:
                if call.args and _literal_names(call.args[0]) is not None:
                    return (owner, _literal_names(call.args[0]))
                if method in {"setdefault", "__setitem__", "pop"} and call.args:
                    name = call.args[0]
                    if isinstance(name, ast.Constant) and isinstance(name.value, str):
                        return (owner, {name.value})
                if not call.args:
                    return (owner, set())
            return (owner, None)
    if (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Call)
        and isinstance(receiver.value.func, ast.Name)
        and receiver.value.func.id in {"vars", "dict"}
        and len(receiver.value.args) == 1
        and not receiver.value.keywords
    ):
        return (receiver.value.args[0], None)
    return None


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
    result = _Scan({}, {}, set(), [], [], {}, set(), set(), [], set(), {}, 0, {})
    calls: list[tuple[str, frozenset[str], str, str]] = []
    setattr_calls: list[
        tuple[str | None, str, str, ast.Call, dict[str, str], dict[str, frozenset[str]] | None, str]
    ] = []
    type_calls: list[tuple[str, str, bool]] = []
    base_refusals: list[tuple[str, ast.AST, dict[str, str], str, str | None]] = []
    decorator_refusals: list[tuple[str, ast.AST, str, dict[str, str], str | None]] = []
    refusals: list[tuple[str, str]] = []
    # The calls that are not in a decorator position, kept until every alias resolves so a
    # call through a re-exported ``dataclass`` is judged after its alias is known.
    dataclass_calls: list[tuple[str | None, str]] = []
    # A call that carries a hash keyword -- ``unsafe_hash=`` or ``frozen=`` -- with a
    # value other than false, and is not the read decorator form. Whether it writes hash
    # authority is decided after every alias resolves, so a call through a re-exported
    # name is judged once that name is known rather than before.
    hash_calls: list[
        tuple[
            str | None,
            str,
            tuple[tuple[str, dict[str, str], dict[str, frozenset[str]] | None, str], ...],
        ]
    ] = []
    # A bare ``super()`` write is read only when the name still resolves to the builtin
    # in its whole scope. A local ``super`` may be assigned after the call, so the check
    # waits until the module walk has seen every binding.
    super_calls: list[tuple[str, str]] = []
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
            evaluation: str | None = None,
        ) -> None:
            def bind(
                name: str, target: str, value: ast.AST | None = None, counted: bool = True
            ) -> None:
                bindings[name] = target
                key = f"{scope}.{name}"
                result.aliases.setdefault(key, set()).add(target)
                if not counted and result.declarations is not None:
                    result.declarations.add(key)
                    if result.declaration_counts is not None:
                        result.declaration_counts[key] = result.declaration_counts.get(key, 0) + 1
                if result.alias_first is not None:
                    result.alias_first.setdefault((key, target), result.order)
                if counted and result.binds is not None:
                    # A name given a new value is counted -- an assignment, a walrus, a
                    # loop target. A declaration -- an import, a class, a function -- is
                    # recorded separately, in ``declarations``, so a name bound once by an
                    # import and once by an assignment still counts twice.
                    result.binds[key] = result.binds.get(key, 0) + 1
                if result.declared is not None and f"{scope}.{name}" in result.declared:
                    # The function declared this name ``global`` or ``nonlocal``, so this
                    # binding writes a name outside the scope the scan walks under: the
                    # value continues in an enclosing scope and the scan cannot follow it.
                    # The local name and the module-level name of that spelling both
                    # resolve to a value the scan cannot read, so a base, decorator or
                    # target either supplies is refused.
                    bindings[name] = _UNRESOLVED
                    # The write lands in the enclosing scope, so every scope that already
                    # holds this name now holds a value the scan cannot read -- the
                    # module-level scope of the same spelling included.
                    for key in [*result.aliases, f"{module}.{name}"]:
                        if key == f"{module}.{name}" or key.endswith(f".{name}"):
                            result.aliases.setdefault(key, set()).add(_UNRESOLVED)

            def refuse(lineno: int, col: int, reason: str) -> None:
                refusals.append((f"{module}:{lineno}:{col + 1}", reason))

            def record_mutation(
                statement: ast.AST,
                reason: str,
                owner: ast.AST | None,
                scope_bindings: dict[str, str],
                before: dict[str, frozenset[str]] | None,
                current_scope: str,
                attribute: str | None = None,
                operation: str = "write",
                value: ast.AST | None = None,
                gated: bool = True,
            ) -> None:
                """Record a write the walk reads, to be resolved after the walk.

                The write's owner is resolved in the module walk's final aliases, so a
                name bound again later is refused instead of read against the value it
                held at this line. A write whose owner is not a plain dotted name is
                refused at once, because the scan has no name to resolve.
                """
                if result.mutations is None:
                    return
                result.mutations.append(
                    _Mutation(
                        f"{module}:{statement.lineno}:{statement.col_offset + 1}",
                        reason,
                        module,
                        owner,
                        scope_bindings,
                        before,
                        current_scope,
                        attribute,
                        operation,
                        result.order,
                        value,
                        gated,
                    )
                )

            def assignment(target: ast.AST, value: ast.AST | None) -> None:
                result.order += 1
                if isinstance(target, ast.Name):
                    if class_name and target.id == "__hash__" and not _none(value):
                        result.writes.append((class_name, "assigned __hash__"))
                    if isinstance(value, ast.Subscript) and target.id not in _AUTHORITY_NAMES:
                        # The value takes one item of a container -- ``BASES[0]`` -- and
                        # which item that is is decided at runtime, so the name is given a
                        # value the scan cannot read rather than guessed at: a base, a
                        # decorator or a target it supplies is refused where it is used. A
                        # class-body authority name keeps its own reading below, which a
                        # configuration value still passes through.
                        bind(target.id, _UNRESOLVED, value)
                        return
                    bind(
                        target.id,
                        _symbol(value, bindings, module) or f"{scope}.{target.id}",
                        value,
                    )
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
                    elif target.attr in {"__hash__", "__eq__", "model_config", "Config"}:
                        owner = _symbol(target.value, bindings, module)
                        resolved = _canonicals(owner, result.aliases) if owner else set()
                        if owner and (_UNRESOLVED in resolved or _PARAMETER in resolved):
                            # The owner resolves to a value the scan does not read -- an
                            # item of a list, or a name the function declared ``global``
                            # -- so the write is refused rather than recorded against a
                            # class the scan cannot name.
                            refusals.append(
                                (
                                    f"{module}:{target.lineno}:{target.col_offset + 1}",
                                    "an attribute assignment whose owner the scan cannot read",
                                )
                            )
                        elif owner and not _none(value):
                            # The owner is resolved once every module is scanned, so a
                            # name bound twice is refused rather than read against the
                            # binding that happened to be current here.
                            record_mutation(
                                target,
                                f"post-definition {target.attr} assignment",
                                target.value,
                                dict(bindings),
                                _bindings_of(
                                    target.value,
                                    result.aliases,
                                    scope,
                                    bindings,
                                    result.class_scopes,
                                ),
                                scope,
                                target.attr,
                                value=value,
                            )
                    else:
                        # A plain attribute assignment gives its owner a value the scan
                        # reads -- ``Holder.K = Known`` -- so a later ``Holder.K`` chain
                        # resolves to that value. The write is recorded and resolved once
                        # every module is scanned, like every other attribute write.
                        record_mutation(
                            target,
                            f"attribute assignment of {target.attr!r}",
                            target.value,
                            dict(bindings),
                            _bindings_of(
                                target.value, result.aliases, scope, bindings, result.class_scopes
                            ),
                            scope,
                            target.attr,
                            value=value,
                            gated=False,
                        )
                elif isinstance(target, ast.Starred):
                    assignment(target.value, value)
                elif isinstance(target, (ast.Tuple, ast.List)) and any(
                    isinstance(element, ast.Starred) for element in target.elts
                ):
                    # A starred element absorbs a variable number of values, so pairing the
                    # target's elements with the value's by position would bind the wrong
                    # value to every name after it. A class attribute in such a target is
                    # refused; a plain name is left unresolved, so a base, decorator or
                    # configuration it supplies is refused where it is used.
                    if _contains_attribute(target):
                        refusals.append(
                            (
                                f"{module}:{target.lineno}:{target.col_offset + 1}",
                                "a starred assignment target the scan cannot read",
                            )
                        )
                    else:
                        for element in target.elts:
                            for name in (
                                item for item in ast.walk(element) if isinstance(item, ast.Name)
                            ):
                                bind(name.id, _UNRESOLVED)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    # A compound target pairs its elements with the value's by position.
                    # Each element is read the same way, so an attribute whose owner is not
                    # a plain name is refused wherever it sits; a class dictionary element
                    # writes a class attribute through the same read as a plain subscript.
                    values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
                    for index, child in enumerate(target.elts):
                        assignment(child, values[index] if index < len(values) else None)
                elif isinstance(target, ast.Subscript):
                    # An item of ``vars(X)`` or ``X.__dict__`` writes an attribute of
                    # ``X``'s class dictionary, so a literal ``__hash__`` or a frozen
                    # setting there writes the class authority just as an attribute
                    # target does. The item is resolved after the walk, like every other
                    # write.
                    write = _dictionary_write(target)
                    if write is not None:
                        owner, names = write
                        if names is None:
                            refusals.append(
                                (
                                    f"{module}:{target.lineno}:{target.col_offset + 1}",
                                    "a class dictionary write the scan cannot read",
                                )
                            )
                        for item_name in names or ():
                            record_mutation(
                                target,
                                f"a class dictionary write of {item_name!r}",
                                owner,
                                dict(bindings),
                                _bindings_of(
                                    owner, result.aliases, scope, bindings, result.class_scopes
                                ),
                                scope,
                                item_name,
                                value=value if len(names) == 1 else None,
                            )

            def loop_target(target: ast.AST, iterable: ast.AST | None) -> None:
                # An attribute target is a binding statement too: a loop or with target
                # that names ``__hash__`` writes it wherever the loop runs. The write is
                # kept for the post-walk resolution, which refuses an owner it cannot read.
                if isinstance(target, ast.Attribute):
                    assignment(target, iterable)
                    return
                if isinstance(target, ast.Subscript) and _dictionary_write(target) is not None:
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
                names: list[ast.Name] = []
                pending: list[ast.AST] = [target]
                while pending:
                    current = pending.pop()
                    if isinstance(current, ast.Name):
                        names.append(current)
                    elif not isinstance(current, ast.Attribute):
                        pending.extend(ast.iter_child_nodes(current))
                for name in names:
                    if not literal:
                        bind(name.id, _UNRESOLVED)
                        continue
                    key = f"{scope}.{name.id}"
                    bindings[name.id] = key
                    if result.binds is not None:
                        # A loop or with target binds the name once and gives it each
                        # element of the literal in turn, so it counts as one binding.
                        result.binds[key] = result.binds.get(key, 0) + 1
                    for element in iterable.elts:
                        result.aliases.setdefault(key, set()).add(
                            _symbol(element, bindings, module) or _UNRESOLVED
                        )
                        if result.alias_first is not None:
                            result.alias_first.setdefault(
                                (key, _symbol(element, bindings, module) or _UNRESOLVED),
                                result.order,
                            )

            for node in body:
                result.order += 1
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
                inspect_calls(expressions, bindings, scope)
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
                        bind(item.asname or item.name, f"{base}.{item.name}", counted=False)
                elif isinstance(node, ast.Import):
                    for item in node.names:
                        local = item.asname or item.name.split(".")[0]
                        bind(local, item.name if item.asname else local, counted=False)
                elif isinstance(node, ast.ClassDef):
                    fqn = f"{scope}.{node.name}"
                    location = f"{module}:{node.lineno}:{node.col_offset + 1}"
                    # A base or decorator of a class nested in a class body is read in
                    # the enclosing function or module scope: a class body's names are
                    # not visible to a nested class, and the scan refuses a name only a
                    # class body binds rather than deciding whether the class statement
                    # itself is evaluated inside that body.
                    statement_scope = (
                        scope.rpartition(".")[0] if class_name and "." in scope else scope
                    )
                    for base in node.bases:
                        # Readability is decided after every module is scanned, so a base
                        # that resolves to a class declared later still resolves. The
                        # location and the bindings are taken here, where they hold. A
                        # subscript base takes one item of a value the scan does not read,
                        # so it is refused at once rather than resolved.
                        if isinstance(base, ast.Subscript):
                            refusals.append(
                                (
                                    f"{module}:{base.lineno}:{base.col_offset + 1}",
                                    "a class base the scan cannot read",
                                )
                            )
                            continue
                        if not _plain_dotted(base):
                            # A base that is not a plain name or a dotted chain -- a call,
                            # a lambda, a conditional expression, a tuple joined together
                            # -- is a value the scan does not follow.
                            refusals.append(
                                (
                                    f"{module}:{base.lineno}:{base.col_offset + 1}",
                                    "a class base the scan cannot read",
                                )
                            )
                            continue
                        base_refusals.append(
                            (
                                f"{module}:{base.lineno}:{base.col_offset + 1}",
                                base,
                                dict(bindings),
                                module,
                                _alias_key(base, scope),
                                fqn,
                                _bindings_of(
                                    base,
                                    result.aliases,
                                    statement_scope,
                                    bindings,
                                    result.class_scopes,
                                ),
                                statement_scope,
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
                                dict(bindings),
                                _alias_key(
                                    decorator.func
                                    if isinstance(decorator, ast.Call)
                                    else decorator,
                                    scope,
                                ),
                                fqn,
                                _bindings_of(
                                    decorator.func
                                    if isinstance(decorator, ast.Call)
                                    else decorator,
                                    result.aliases,
                                    statement_scope,
                                    bindings,
                                    result.class_scopes,
                                ),
                                statement_scope,
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
                                result.symbols,
                                bindings,
                                _alias_key(statement.value, scope),
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
                    if fqn in result.classes:
                        # The same class name declared twice in one scope leaves only the
                        # last definition, so a descendant, a frozen configuration or a
                        # declared hash in the first could be dropped unread.
                        refusals.append(
                            (
                                location,
                                f"a second declaration of {fqn} the scan cannot read",
                            )
                        )
                    result.classes[fqn] = _Class(node, location, bases, decorators)
                    result.symbols.add(fqn)
                    bind(node.name, fqn, counted=False)
                    if result.class_scopes is not None:
                        result.class_scopes.add(fqn)
                    # A class body sees its own names, but not the names of an enclosing
                    # class body: a method or a nested class body resolves a free name in
                    # the module or function scope, never in the enclosing class. The
                    # body's bindings therefore start from the enclosing non-class scope,
                    # while the bases just recorded were read in the scope that evaluates
                    # the class statement.
                    outer = closure if class_name and closure is not None else bindings
                    walk(node.body, outer.copy(), fqn, fqn, outer.copy())
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fqn = f"{scope}.{node.name}"
                    result.symbols.add(fqn)
                    if class_name and node.name == "__hash__":
                        result.writes.append((class_name, "defined __hash__"))
                    bind(node.name, fqn, counted=False)
                    local = (closure if class_name and closure is not None else bindings).copy()
                    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                    args.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg)
                    for arg in args:
                        local[arg.arg] = f"{fqn}.<locals>.{arg.arg}"
                        # A parameter holds whatever the caller passes, so a suite that
                        # rebinds it adds a value to the parameter rather than replacing
                        # it, and a base the parameter supplies stays unreadable.
                        result.aliases.setdefault(local[arg.arg], set()).add(_PARAMETER)
                    walk(node.body, local, f"{fqn}.<locals>")
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if node.value is not None:
                        for target in targets:
                            assignment(target, node.value)
                elif isinstance(node, (ast.Global, ast.Nonlocal)):
                    # A name the function declares stays in the enclosing scope, so the
                    # scan records it and refuses a value it supplies rather than reading
                    # the local binding as the whole story.
                    for name in node.names:
                        if result.declared is not None:
                            result.declared.add(f"{scope}.{name}")
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
                            record_mutation(
                                target,
                                f"deleted {target.attr}",
                                target.value,
                                dict(bindings),
                                _bindings_of(
                                    target.value,
                                    result.aliases,
                                    scope,
                                    bindings,
                                    result.class_scopes,
                                ),
                                scope,
                                target.attr,
                                "delete",
                            )
                elif not isinstance(node, ast.Expr):
                    if isinstance(node, (ast.For, ast.AsyncFor)):
                        loop_target(node.target, node.iter)
                    elif isinstance(node, (ast.With, ast.AsyncWith)):
                        for item in node.items:
                            if item.optional_vars is not None:
                                loop_target(item.optional_vars, item.context_expr)
                    # Inspect declarations in conditional, loop, try and match suites
                    # without pretending to decide which branch executes at runtime. A
                    # nested suite bound the name to a new value, so the name resolves to
                    # every value it was given, the value it held before the suite among
                    # them: an enclosing scope's binding, or, when nothing bound it, the
                    # module global or builtin it names. A base, decorator or configuration
                    # it supplies is read only when each of those values is readable. A
                    # declaration made in more than one alternative is one binding on
                    # either path, so the declaration counts keep the largest number one
                    # alternative adds rather than the sum across alternatives; a
                    # declaration made twice inside one alternative still counts twice.
                    base_declarations = dict(result.declaration_counts or {})
                    alternative_added: dict[str, int] = {}
                    for statements in _suite_bodies(node, module):
                        before_suite = dict(result.declaration_counts or {})
                        inner = bindings.copy()
                        walk(statements, inner, scope, class_name, closure)
                        for name, value in inner.items():
                            prior = bindings.get(name)
                            if value == prior:
                                continue
                            key = f"{scope}.{name}"
                            before = prior or _symbol(ast.Name(id=name), {}, module)
                            if before and before != key:
                                result.aliases.setdefault(key, set()).add(before)
                            bindings[name] = key
                        for key, total in (result.declaration_counts or {}).items():
                            added = total - before_suite.get(key, 0)
                            if added > 0:
                                alternative_added[key] = max(alternative_added.get(key, 0), added)
                    if result.declaration_counts is not None:
                        for key, added in alternative_added.items():
                            result.declaration_counts[key] = base_declarations.get(key, 0) + added

        def inspect_calls(
            expressions: list[ast.expr], bindings: dict[str, str], scope: str
        ) -> None:
            for expression in expressions:
                for call in (item for item in ast.walk(expression) if isinstance(item, ast.Call)):
                    # The callee and arguments are checked against the bindings that
                    # hold at THIS call. Taking them after walking every statement in
                    # the expression would let a later binding in the suite supply them.
                    call_bindings = dict(bindings)
                    call_order = result.order
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
                        # scan cannot follow, such as ``type(Known).__setattr__``. A call
                        # whose first argument is a plain name or dotted attribute could
                        # write a class attribute, so it is refused unless it is the bare
                        # ``super()`` form, which writes an instance field. ``super`` given
                        # arguments can name any class and its metaclass, as in
                        # ``super(type(Known), Known).__setattr__``, so it is refused too.
                        receiver = call.func.value
                        resolved = (
                            _visible_candidates(
                                receiver.func, result.aliases, module, bindings, scope
                            )
                            if isinstance(receiver, ast.Call)
                            and not receiver.args
                            and not receiver.keywords
                            else None
                        )
                        if resolved and resolved <= {"builtins.super"}:
                            super_calls.append((location, f"{scope}.super"))
                            # The bare ``super()`` form writes an instance field through the
                            # class's own ``__setattr__``, not a class authority, so it is
                            # read. The name must resolve to nothing but ``builtins.super``:
                            # a name bound more than once -- a local ``super`` beside the
                            # builtin -- could name any object, so it is refused.
                            continue
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
                    if not call.args and not call.keywords:
                        # A bare call -- ``ConfigDict()``, ``Field()`` -- is a value the
                        # scan does not follow as a write. Only a call with arguments is
                        # read for the class it may write.
                        continue
                    if call.args and (
                        isinstance(call.args[0], ast.Constant)
                        and not isinstance(call.args[0].value, str)
                    ):
                        # A call whose first argument is a literal -- ``Field(1,
                        # frozen=True)`` -- has no class owner to write.
                        continue
                    # A setattr-like call is resolved after every module is scanned, so a
                    # re-exported name still resolves. The owner, the location and what the
                    # target name held here are taken now, where the bindings are, so a
                    # target bound after the call is told from one bound before it.
                    target = call.args[0] if call.args else None
                    setattr_calls.append(
                        (
                            function,
                            location,
                            owner or "",
                            call,
                            dict(bindings),
                            _bindings_of(
                                target, result.aliases, scope, bindings, result.class_scopes
                            )
                            if target is not None
                            else None,
                            scope,
                        )
                    )
                    if not decorated_dataclass:
                        # ``dataclass`` used as a decorator is read; used anywhere else it
                        # rewrites a class's fields and hash authority, and the scan does
                        # not follow the argument it is given, so every such call is
                        # refused. Whether the name IS ``dataclass`` is decided after every
                        # module is scanned: a call through a re-exported alias is judged
                        # once that alias resolves, not before.
                        dataclass_calls.append((function, location))
                        if any(
                            kw.arg in _HASH_KEYWORDS and not _false(kw.value)
                            for kw in call.keywords
                        ) and not (canonical & (_CONFIG_CALLS | _FIELD_CALLS)):
                            # ``ConfigDict(...)`` is the configuration form the scan
                            # reads, and a field declaration's ``frozen`` keyword is a
                            # field setting; every other call with a hash keyword is
                            # followed only through its arguments, below.
                            if any(
                                isinstance(argument, ast.Starred) for argument in call.args
                            ) or any(kw.arg is None for kw in call.keywords):
                                refusals.append((location, "a decorator call the scan cannot read"))
                                continue
                            arguments = []
                            for argument in call.args:
                                if isinstance(argument, ast.Starred):
                                    arguments.append(
                                        (
                                            _UNRESOLVED,
                                            call_bindings,
                                            None,
                                            scope,
                                            call_order,
                                        )
                                    )
                                    continue
                                if isinstance(argument, (ast.Subscript, ast.Lambda)) or (
                                    isinstance(argument, ast.Call)
                                ):
                                    arguments.append(
                                        (
                                            _UNRESOLVED,
                                            call_bindings,
                                            None,
                                            scope,
                                            call_order,
                                        )
                                    )
                                    continue
                                if isinstance(argument, ast.Constant):
                                    arguments.append(
                                        (
                                            f"builtins.{type(argument.value).__name__}",
                                            call_bindings,
                                            None,
                                            scope,
                                            call_order,
                                        )
                                    )
                                    continue
                                symbol = _symbol(argument, call_bindings, module)
                                if symbol is None:
                                    arguments.append(
                                        (_UNRESOLVED, call_bindings, None, scope, call_order)
                                    )
                                    continue
                                arguments.append(
                                    (
                                        symbol,
                                        call_bindings,
                                        _bindings_of(
                                            argument,
                                            result.aliases,
                                            scope,
                                            call_bindings,
                                            result.class_scopes,
                                        ),
                                        scope,
                                        call_order,
                                    )
                                )
                            for keyword in call.keywords:
                                if keyword.arg is None:
                                    arguments.append(
                                        (
                                            _UNRESOLVED,
                                            call_bindings,
                                            None,
                                            scope,
                                            call_order,
                                        )
                                    )
                                    continue
                                if keyword.arg in _HASH_KEYWORDS:
                                    # The hash setting's own value -- ``True``, a
                                    # frozen option -- is not a class the call receives,
                                    # so it is not read as one.
                                    continue
                                argument = keyword.value
                                if isinstance(argument, (ast.Subscript, ast.Lambda)) or (
                                    isinstance(argument, ast.Call)
                                ):
                                    arguments.append(
                                        (
                                            _UNRESOLVED,
                                            call_bindings,
                                            None,
                                            scope,
                                            call_order,
                                        )
                                    )
                                    continue
                                if isinstance(argument, ast.Constant):
                                    arguments.append(
                                        (
                                            f"builtins.{type(argument.value).__name__}",
                                            call_bindings,
                                            None,
                                            scope,
                                            call_order,
                                        )
                                    )
                                    continue
                                symbol = _symbol(argument, call_bindings, module)
                                arguments.append(
                                    (
                                        symbol or _UNRESOLVED,
                                        call_bindings,
                                        _bindings_of(
                                            argument,
                                            result.aliases,
                                            scope,
                                            call_bindings,
                                            result.class_scopes,
                                        ),
                                        scope,
                                        call_order,
                                    )
                                )
                            hash_calls.append(
                                (
                                    function,
                                    location,
                                    tuple(arguments),
                                )
                            )
                        elif any(
                            kw.arg in _HASH_KEYWORDS and not _false(kw.value)
                            for kw in _hash_keywords_of(call.func, module, bindings, result.aliases)
                        ):
                            # ``partial(decorate, unsafe_hash=True)(Known)`` applies a
                            # curried call that carries a hash keyword, which the scan
                            # does not follow.
                            refusals.append((location, "a decorator call the scan cannot read"))

        walk(tree.body, {}, module)
    for location, key in super_calls:
        if key in result.aliases or (result.binds or {}).get(key):
            # A local ``super`` shadows the builtin for every reference in its scope,
            # including one written before the assignment, so the write target could be
            # any object. It is refused rather than read as the builtin.
            refusals.append((location, "a setattr call the scan cannot read"))
    # A base is read only when its spine resolves to a class the scan inventories,
    # whichever module declares it, so this runs once every module has been scanned.
    for location, base, bindings, module, key, owner, before, scope in base_refusals:
        if not _readable_base(
            base,
            result.aliases,
            module,
            set(result.classes),
            result.symbols,
            bindings,
            key,
            owner,
            before,
            scope,
            result.binds,
            result.class_scopes,
            result.declarations,
            result.declaration_counts,
            result.alias_first,
        ):
            refusals.append((location, "a class base the scan cannot read"))
    for location, decorator, module, bindings, key, owner, before, scope in decorator_refusals:
        if not _readable_decorator(
            decorator,
            result.aliases,
            module,
            result.symbols,
            bindings,
            key,
            owner,
            before,
            scope,
            result.binds,
            result.class_scopes,
            result.declarations,
            result.declaration_counts,
            result.alias_first,
        ):
            refusals.append((location, "a decorator the scan cannot read"))
    for function, location in dataclass_calls:
        if function and "dataclasses.dataclass" in _canonicals(function, result.aliases):
            refusals.append((location, "a dataclass call that is not a decorator"))
    declared_classes = set(result.classes)
    for function, location, arguments in hash_calls:
        if function and "dataclasses.dataclass" in _canonicals(function, result.aliases):
            # A ``dataclass`` call outside a decorator position is refused above; it does
            # not need a second refusal here.
            continue
        unreadable = False
        passes_a_class = False
        for symbol, bindings, before, scope, order in arguments:
            if symbol == _UNRESOLVED:
                unreadable = True
                continue
            if not _bindings_before(result, symbol, bindings, before, scope, order):
                unreadable = True
            if _canonicals(symbol, result.aliases) & declared_classes:
                passes_a_class = True
        if unreadable or passes_a_class:
            # The call passes a class the scan inventories and a hash keyword that is not
            # false, so it can turn on a generated hash for that class. The scan does not
            # follow what the call does with it, so the call is refused rather than read.
            refusals.append((location, "a decorator call the scan cannot read"))
    for function, location, builds_a_class in type_calls:
        if not builds_a_class or not function:
            continue
        if "builtins.type" in _canonicals(function, result.aliases):
            refusals.append((location, "a class built with type()"))
    for function, expected, owner, description in calls:
        if _canonicals(function, result.aliases) & expected:
            result.writes.append((owner, description))
    for function, location, owner, call, bindings, before, scope in setattr_calls:
        canonical = _canonicals(function, result.aliases)
        if not (canonical & _SETATTR):
            continue
        first = call.args[0] if call.args else None
        second = call.args[1] if len(call.args) >= 2 else None
        if (
            len(call.args) != 3
            or call.keywords
            or any(isinstance(argument, ast.Starred) for argument in call.args)
            or not _plain_dotted(first)
            or (
                first is not None
                and not _read_once(
                    first,
                    result.aliases,
                    bindings,
                    before,
                    scope,
                    result.binds,
                    result.class_scopes,
                    result.declarations,
                    result.declaration_counts,
                    result.alias_first,
                )
            )
            or not isinstance(second, ast.Constant)
            or not isinstance(second.value, str)
        ):
            # A setattr whose owner, attribute and value the scan cannot read as three
            # literal arguments is refused, and so is every delattr, which takes two:
            # writing or removing a class authority through it would go unread.
            refusals.append((location, "a setattr call the scan cannot read"))
        elif second.value in _CLASS_AUTHORITY:
            # Writing ``__bases__``, ``__class__``, ``__mro__`` or ``model_config``
            # through ``setattr`` rebinds what the class is or the configuration it
            # inherits after its declaration, so it is refused rather than reported as a
            # class write the inventory could follow.
            refusals.append((location, f"a setattr write of {second.value!r} the scan cannot read"))
        elif second.value in {"__hash__", "__eq__"} and owner and not _none(call.args[2]):
            result.writes.append((owner, f"setattr {second.value}"))
        elif owner:
            result.writes.append((owner, "setattr"))
    for mutation in result.mutations or ():
        if (
            mutation.operation == "write"
            and mutation.attribute
            and (mutation.attribute not in _AUTHORITY_NAMES or not _none(mutation.value))
        ):
            bindings = dict(mutation.bindings)
            symbol = _symbol(mutation.owner, bindings, mutation.module)
            if symbol is not None:
                key = f"{symbol}.{mutation.attribute}"
                had_binding = key in result.aliases
                if mutation.value is not None and isinstance(
                    mutation.value, (ast.Tuple, ast.List, ast.Set)
                ):
                    values = {
                        (_symbol(element, bindings, mutation.module) or _UNRESOLVED)
                        for element in mutation.value.elts
                        if element is not None
                    }
                else:
                    values = {
                        (
                            _symbol(mutation.value, bindings, mutation.module)
                            if mutation.value is not None
                            else _UNRESOLVED
                        )
                        or _UNRESOLVED
                    }
                if not had_binding and result.alias_first is not None and _UNRESOLVED not in values:
                    result.alias_first.setdefault((key, _UNRESOLVED), mutation.order)
                result.aliases.setdefault(key, set()).update(values)
                if result.alias_first is not None:
                    for value_symbol in values:
                        result.alias_first.setdefault((key, value_symbol), mutation.order)
                if result.declaration_counts is not None:
                    result.declaration_counts[key] = result.declaration_counts.get(key, 0) + (
                        0 if had_binding else 1
                    )
        if (
            mutation.gated
            and mutation.attribute in _AUTHORITY_NAMES
            and not (
                _read_once(
                    mutation.owner,
                    result.aliases,
                    mutation.bindings,
                    mutation.before,
                    mutation.scope,
                    result.binds,
                    result.class_scopes,
                    result.declarations,
                    result.declaration_counts,
                    result.alias_first,
                )
                and _mutation_target_is_readable(mutation, result)
            )
        ):
            refusals.append((mutation.location, f"{mutation.kind} the scan cannot read"))
            if mutation.attribute in _AUTHORITY_NAMES:
                continue
        owner = _symbol(mutation.owner, mutation.bindings, mutation.module)
        if owner and mutation.attribute in _AUTHORITY_NAMES:
            resolved = _canonicals(owner, result.aliases)
            if _UNRESOLVED in resolved or _PARAMETER in resolved:
                # The owner chain takes a value the scan cannot read -- a loop or
                # ``with`` target, a subscript, or a parameter -- so the write could land
                # on any class. It is refused rather than reported against a name.
                refusals.append((mutation.location, f"{mutation.kind} the scan cannot read"))
                continue
        if owner and mutation.attribute in _AUTHORITY_NAMES:
            result.writes.append((owner, mutation.kind))
    result.refusals = refusals
    return result


def _mutation_target_is_readable(mutation: _Mutation, scan: _Scan) -> bool:
    """Whether every class an authority write can reach is one the scan can name.

    An owner name can be bound more than once in the enclosing scopes, and a dotted chain
    reached through a class attribute alias -- ``Holder.K``, where ``K`` was assigned a
    class -- is exactly how such a write reaches a family class. Each value of the owner
    root, the rest of the chain appended, is expanded through the aliases; a value the
    scan cannot read, a class outside the scope it proves, or an attribute of one the
    scan never saw assigned is refused rather than read against whichever value happened
    to be current when the write was walked.
    """
    owner = _symbol(mutation.owner, mutation.bindings, mutation.module)
    if not owner:
        return False
    candidates = _canonicals(owner, scan.aliases)
    if not candidates or candidates & {_UNRESOLVED, _PARAMETER}:
        return False
    return all(
        _readable_symbol(candidate, mutation.module, set(scan.classes)) or candidate in scan.symbols
        for candidate in candidates
    )


def _selected(scan: _Scan, roots: frozenset[str] = _ROOTS) -> frozenset[str]:
    selected = set(roots & scan.classes.keys())
    while True:
        # A class base names a class directly; an attribute chain that a write supplied
        # is already expanded through the aliases, so only the class declarations added
        # by the previous pass need to be considered here.
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
    # Both import roots of the wheel: ``lionhub`` holds packages that moved out of
    # ``lionagi`` and still subclass its models.
    sources = {}
    paths = [(_ROOT, p) for p in sorted((_ROOT / "lionagi").rglob("*.py"))]
    lionhub_src = _ROOT / "lionhub" / "src"
    paths += [(lionhub_src, p) for p in sorted((lionhub_src / "lionhub").rglob("*.py"))]
    for base, path in paths:
        parts = list(path.relative_to(base).with_suffix("").parts)
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


def test_the_scan_reads_both_import_roots():
    modules = _production_sources()
    assert "lionagi.protocols.generic.element" in modules
    assert any(module.startswith("lionhub.") for module in modules)
    assert not any(module.startswith("src.") for module in modules)


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
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors
    assert any(
        "post-definition __hash__ assignment" in error
        or "loop, with or comprehension target the scan cannot read" in error
        for error in errors
    ), errors


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
    # ``Local`` takes a base the scan cannot read: the ``shadow`` parameter named
    # ``HashableModel`` shadows the import, so the base resolves to the parameter and
    # not to the family root. ``Other`` takes ``Alias`` as a base, and ``Alias`` was bound
    # twice -- to the family root and to ``object`` -- so which value the class received
    # is decided by runtime order the scan does not model, and the base is refused rather
    # than resolved to one of them. Neither class is authority of the family: ``Other``
    # declares its own ``__hash__`` but is outside it, and nothing else in the module is
    # authority of it.
    assert _violations(scan, {*_ROOTS, "sample.Known"}) == [
        "sample:12:13: a class base the scan cannot read",
        "sample:8:17: a class base the scan cannot read",
    ]


# A shape a resolved base, decorator or write target must not be read from, with the
# refusal its own rule emits. Each sample resolves a name the scan cannot know the value
# of -- a class name a scope defines twice, a write target that is not a plain dotted
# name, a name given a second value or bound after its use, a name taken from a
# subscript, a name a function declares ``global`` or ``nonlocal``, ``super`` given a
# second value, or a keyword call that passes a class the scan inventories. Undoing that
# one rule turns its case red, so a shape refused only by a neighbouring rule fails its
# case rather than passing untested.
_UNRESOLVED_SHAPE_CASES: list[tuple[str, str, str, dict[str, tuple[str, str]] | None]] = [
    (
        "I01",
        "a second declaration of sample.Added the scan cannot read",
        "if True:\n    class Added(HashableModel): pass\nelse:\n    class Added: pass",
        None,
    ),
    (
        "I01-frozen",
        "a second declaration of sample.Added the scan cannot read",
        "from pydantic import ConfigDict\nif True:\n    class Added(HashableModel):\n"
        "        model_config = ConfigDict(frozen=True)\nelse:\n"
        "    class Added(HashableModel): pass",
        None,
    ),
    (
        "I02",
        "a setattr call the scan cannot read",
        "class Holder(HashableModel): pass\nHolder.K = Holder\n"
        "setattr([Holder][0].K, '__hash__', object.__hash__)",
        None,
    ),
    (
        "I03-rebound",
        "a class base the scan cannot read",
        "Base = HashableModel\nBase = object\nclass Added(Base): pass",
        None,
    ),
    (
        "I03-after-use",
        "a class base the scan cannot read",
        "Base = HashableModel\nclass Added(Base): pass\nBase = object",
        None,
    ),
    (
        "I03-decorator",
        "a decorator the scan cannot read",
        "from dataclasses import dataclass\ndec = dataclass\ndec = dataclass\n@dec\n"
        "class Added(HashableModel): pass",
        None,
    ),
    (
        "I04-container",
        "a class base the scan cannot read",
        "BASES = HashableModel\nBase = BASES[0]\nclass Added(Base): pass",
        None,
    ),
    (
        "I04-inline",
        "a class base the scan cannot read",
        "Base = object\nBase = (HashableModel,)[0]\nclass Added(Base): pass",
        None,
    ),
    (
        "I05",
        "a decorator call the scan cannot read",
        "from helpers import decorate\nclass Added(HashableModel): pass\n"
        "decorate(cls=Added, unsafe_hash=True)",
        {"helpers": ("", "def decorate(*, cls, **kw):\n    return cls\n")},
    ),
    (
        "I06-global",
        "a class base the scan cannot read",
        "def make():\n    global Base\n    Base = HashableModel\n    class Added(Base): pass",
        None,
    ),
    (
        "I06-nonlocal",
        "a class base the scan cannot read",
        "def outer():\n    Base = HashableModel\n    def inner():\n"
        "        nonlocal Base\n        Base = object\n    class Added(Base): pass",
        None,
    ),
    (
        "I07-rebound-super",
        "a setattr call the scan cannot read",
        "import builtins\nsuper = object\nsuper = builtins.super\n"
        "class Added(HashableModel): pass\n"
        "def make():\n    super().__setattr__('__hash__', object.__hash__)",
        None,
    ),
    (
        "I07-local-rebound-super",
        "a setattr call the scan cannot read",
        "class Known(HashableModel): pass\ndef make():\n"
        "    super().__setattr__('__hash__', object.__hash__)\n    super = object",
        None,
    ),
    (
        "I03a-duplicate-import",
        "a class base the scan cannot read",
        "from builtins import object as Base\n"
        "from lionagi.models import HashableModel as Base\nclass Added(Base): pass",
        None,
    ),
    (
        "J01a-duplicate-import-inner",
        "a class base the scan cannot read",
        "from builtins import object as Base\n"
        "from lionagi.models import HashableModel as Base\n"
        "def make():\n    class Added(Base): pass",
        None,
    ),
    (
        "J02a-outer-class-scope",
        "a class base the scan cannot read",
        "class Outer:\n    Base = object\n    class Added(Base): pass",
        None,
    ),
    (
        "J02b-method-class-scope",
        "a class base the scan cannot read",
        "class Outer:\n    Base = object\n    def make(self):\n        class Added(Base): pass",
        None,
    ),
    (
        "J02c-outer-class-base",
        "a class base the scan cannot read",
        "class Outer:\n    Base = object\n    class Added(Base): pass",
        None,
    ),
    (
        "I03b-rebound-write-target",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nclass Holder:\n    pass\n"
        "Holder.K = Known\nHolder.K.__hash__ = object.__hash__\nHolder.K = object",
        None,
    ),
    (
        "J03a-class-alias",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nclass Holder:\n    K = Known\n"
        "Holder.K.__hash__ = object.__hash__",
        None,
    ),
    (
        "J03b-class-alias-delete",
        "deleted __hash__",
        "class Known(HashableModel): pass\nclass Holder:\n    K = Known\ndel Holder.K.__hash__",
        None,
    ),
    (
        "J03c-post-definition-alias",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nclass Holder:\n    pass\n"
        "Holder.Target = Known\nHolder.Target.__hash__ = object.__hash__",
        None,
    ),
    (
        "J04a-attribute-alias",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nclass Holder:\n    pass\n"
        "Holder.K = Known\nHolder.K.__hash__ = object.__hash__",
        None,
    ),
    (
        "J04b-rebound-attribute",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nHolder = object\n"
        "class Holder:\n    K = None\nHolder.K = Known\n"
        "Holder.K.__hash__ = object.__hash__",
        None,
    ),
    (
        "J04c-dictionary-alias",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nclass Holder:\n    pass\n"
        "Holder.__dict__['K'] = Known\nHolder.K.__hash__ = object.__hash__",
        None,
    ),
    (
        "J04d-tuple-alias",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nclass Holder:\n    pass\n"
        "Holder.K, Holder.L = Known, None\nHolder.K.__hash__ = object.__hash__",
        None,
    ),
    (
        "J04e-with-target",
        "the scan cannot read",
        "class Known(HashableModel): pass\nclass Holder:\n    pass\n"
        "with nullcontext(Known) as Holder.K:\n    pass\n"
        "Holder.K.__hash__ = object.__hash__",
        None,
    ),
    (
        "J04f-loop-target",
        "post-definition __hash__ assignment",
        "class Known(HashableModel): pass\nclass Holder:\n    pass\n"
        "for Holder.K in (Known,):\n    pass\nHolder.K.__hash__ = object.__hash__",
        None,
    ),
    (
        "J04g-attribute-base",
        "a class base the scan cannot read",
        "class Known(HashableModel): pass\nclass Holder:\n    Target = None\n"
        "Holder.Target = Known\nclass Added(Holder.Target): pass",
        None,
    ),
    (
        "J05a-lambda-decorator",
        "a decorator call the scan cannot read",
        "class Known(HashableModel): pass\nBase = Known\nBase = lambda: None\n"
        "decorate(Base, unsafe_hash=True)",
        {"helpers": ("", "def decorate(*a, **k):\n    return a[0]\n")},
    ),
    (
        "J05b-lambda-super",
        "a setattr call the scan cannot read",
        "class Known(HashableModel): pass\ndef make():\n    super = lambda: Known\n"
        "    super().__setattr__('__hash__', object.__hash__)",
        None,
    ),
    (
        "J06a-star-args",
        "a decorator call the scan cannot read",
        "class Known(HashableModel): pass\nARGS = [Known]\ndecorate(*ARGS, unsafe_hash=True)",
        {"helpers": ("", "def decorate(*a, **k):\n    return a[0]\n")},
    ),
    (
        "J06b-star-kwargs",
        "a decorator call the scan cannot read",
        "class Known(HashableModel): pass\nKW = {'cls': Known}\ndecorate(**KW, unsafe_hash=True)",
        {"helpers": ("", "def decorate(*a, **k):\n    return a[0]\n")},
    ),
    (
        "J06c-partial",
        "a decorator call the scan cannot read",
        "from functools import partial\nclass Known(HashableModel): pass\n"
        "partial(decorate, unsafe_hash=True)(Known)",
        {"helpers": ("", "def decorate(*a, **k):\n    return a[0]\n")},
    ),
    (
        "J06d-subscript-argument",
        "a decorator call the scan cannot read",
        "class Known(HashableModel): pass\nCLASSES = [Known]\n"
        "decorate(CLASSES[0], unsafe_hash=True)",
        {"helpers": ("", "def decorate(*a, **k):\n    return a[0]\n")},
    ),
    (
        "J07-call-argument",
        "a decorator call the scan cannot read",
        "class Known(HashableModel): pass\ndef pick():\n    return Known\n"
        "decorate(pick(), unsafe_hash=True)",
        {"helpers": ("", "def decorate(*a, **k):\n    return a[0]\n")},
    ),
]


@pytest.mark.parametrize(
    "case_id,marker,snippet,other",
    [
        pytest.param(case_id, marker, snippet, other, id=case_id)
        for case_id, marker, snippet, other in _UNRESOLVED_SHAPE_CASES
    ],
)
def test_a_name_whose_value_the_scan_cannot_resolve_is_refused(case_id, marker, snippet, other):
    """A class base, decorator or write target the scan cannot resolve to one value is
    refused rather than guessed at, and its own rule is what refuses it. The name may be
    one a scope defines twice, one given a second value or bound after its use, one taken
    from a subscript, one a function declares ``global`` or ``nonlocal``, a ``super``
    given a second value, a write target that is not a plain dotted name, or a keyword
    call that passes a class the scan inventories. Each is a shape whose value is decided
    at runtime, so reading one would let a hashable mutable descendant or a hash authority
    go unread."""
    scan = _sample("from lionagi.models import HashableModel\n" + snippet, other)
    errors = _violations(scan, {*_ROOTS, "sample.Added"})
    assert any(marker in error for error in errors), (case_id, marker, errors)


def test_a_function_scope_import_fallback_binding_a_base_is_left_refused():
    """A function-scope try/except that binds a base in both arms is refused, and the
    refusal is kept rather than resolved. Which arm runs depends on whether a module is
    importable on the host, which the scan does not model, so the two values are not
    distinguishable from the source. The framework tree holds no such form: every
    function-scope ``except ImportError`` site is checked, and none of them binds a plain
    name that a later class base or decorator reads, so the refusal costs the tree
    nothing."""
    scan = _sample(
        "from lionagi.models import HashableModel\n"
        "def make():\n    try:\n        from pkg import Base\n"
        "    except ImportError:\n        Base = object\n    class Added(Base): pass"
    )
    errors = _violations(scan, {*_ROOTS, "sample.make.<locals>.Added"})
    assert any("a class base the scan cannot read" in error for error in errors), errors


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
    (
        "G01",
        "class Known(HashableModel):\n    pass\n*_, Known.__hash__ = None, None, object.__hash__\n",
    ),
    (
        "G02",
        "*_, Base = object, object, HashableModel\n"
        "class Known(Base):\n    __hash__ = object.__hash__\n",
    ),
    (
        "G03",
        "Base = object\nif True:\n    Base = HashableModel\n"
        "class Known(Base):\n    __hash__ = object.__hash__\n",
    ),
    (
        "G04",
        "class Known(HashableModel):\n    pass\nclass Unrelated:\n    pass\n"
        "Target = Unrelated\nif True:\n    Target = Known\n"
        "Target.__hash__ = object.__hash__\n",
    ),
    (
        "G05",
        "from dataclasses import dataclass\nfrom typing import runtime_checkable\n"
        "dec = runtime_checkable\nif True:\n    dec = dataclass\n"
        "@dec(unsafe_hash=True)\nclass Known(HashableModel):\n    pass\n",
    ),
    (
        "G06",
        "BASES = (HashableModel,)\nBase = object\nBase = BASES[0]\n"
        "class Known(Base):\n    __hash__ = object.__hash__\n",
    ),
    (
        "G07",
        "from dataclasses import dataclass as dec\nfrom helpers import add_hash as dec\n"
        "@dec\nclass Known(HashableModel):\n    pass\n",
    ),
    (
        "G08",
        "make_cfg = dict\ndef make_cfg():\n    return {'frozen': True}\n"
        "class Known(HashableModel):\n    model_config = make_cfg()\n",
    ),
    (
        "G09",
        "class BaseCfg:\n    frozen = True\n"
        "class Known(HashableModel):\n    class Config(BaseCfg):\n        pass\n",
    ),
    (
        "G10",
        "class Known(HashableModel):\n    pass\n"
        "type(Known).__delattr__(Known, '__hash__')\n"
        "type(Known).__setattr__(*(Known, '__hash__', object.__hash__))\n"
        "deleter = object.__delattr__\ndeleter(Known, '__hash__')\n",
    ),
    (
        "G11",
        "class Known(HashableModel):\n    pass\nsetattr(Known, '__hash__', *(object.__hash__,))\n",
    ),
    (
        "G12",
        "class Known(HashableModel):\n    pass\n"
        "setattr(Known, '__hash__', object.__hash__, frozen=True)\n",
    ),
    (
        "G13",
        "helper = None\nBase = helper\nBase = HashableModel\nclass Known(Base):\n    pass\n",
    ),
    (
        "G14",
        "class Known(HashableModel):\n    pass\n"
        "deleter = object.__delattr__\ndeleter(Known, '__hash__')\n",
    ),
    (
        "H01",
        "class Known(HashableModel):\n    pass\n"
        "super(type(Known), Known).__setattr__('__hash__', object.__hash__)\n",
    ),
    (
        "H02",
        "class M(type):\n    pass\nclass Known(HashableModel):\n    pass\n"
        "super(M, M).__delattr__(Known, '__hash__')\n",
    ),
    (
        "H03",
        "def make():\n    if True:\n        Base = HashableModel\n"
        "    class Added(Base):\n        __hash__ = object.__hash__\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "H04",
        "from dataclasses import dataclass\n"
        "def make():\n    if True:\n        dec = dataclass\n"
        "    @dec(unsafe_hash=True)\n    class Added(HashableModel):\n        pass\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "H05",
        "class Known(HashableModel):\n    pass\n"
        "def mutate():\n    if True:\n        Target = Known\n"
        "    Target.__hash__ = object.__hash__\n",
    ),
    (
        "H06",
        "if True:\n    id = HashableModel\n"
        "class Added(id):\n    __hash__ = object.__hash__\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "H07",
        "def shadow(HashableModel):\n    if False:\n        HashableModel = object\n"
        "    class Local(HashableModel):\n        __hash__ = object.__hash__\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "H08",
        "from dataclasses import dataclass\nfrom helpers import add_hash as dec\n"
        "def make():\n    if False:\n        dec = dataclass\n"
        "    @dec\n    class Added(HashableModel):\n        pass\n"
        "class Known(HashableModel):\n    pass\n",
    ),
    (
        "H09",
        "import helpers as mod\nimport dataclasses as mod\n"
        "@mod.dataclass\nclass Known(HashableModel):\n    pass\n",
    ),
]

# The refusal each shape must produce on its own. The scan refuses a shape for one
# reason; several shapes are refused by more than one rule at once, and a shape that
# only trips a neighbouring rule would leave the rule it names untested. The marker is
# the refusal text the named rule emits, so undoing that rule alone turns its case red.
_REFUSAL_MARKERS: dict[str, str] = {
    "G01": "a starred assignment target the scan cannot read",
    "G02": "a class base the scan cannot read",
    "G03": "assigned __hash__",
    "G04": "post-definition __hash__ assignment",
    "G05": "generated hash or frozen configuration",
    "G06": "a class base the scan cannot read",
    "G07": "a decorator the scan cannot read",
    "G08": "a model_config value the scan cannot read",
    "G09": "a Config body the scan cannot read",
    "G10": "a setattr call the scan cannot read",
    "G11": "a setattr call the scan cannot read",
    "G12": "a setattr call the scan cannot read",
    "G13": "a class base the scan cannot read",
    "G14": "a setattr call the scan cannot read",
    "H01": "a setattr call the scan cannot read",
    "H02": "a setattr call the scan cannot read",
    "H03": "a class base the scan cannot read",
    "H04": "a decorator the scan cannot read",
    "H05": "post-definition __hash__ assignment",
    "H06": "assigned __hash__",
    "H07": "a class base the scan cannot read",
    "H08": "a decorator the scan cannot read",
    "H09": "a decorator the scan cannot read",
}


_TYPE_STATEMENT_CASES = {"C22", "C23"}


@pytest.mark.parametrize(
    "case_id,snippet",
    [
        pytest.param(
            case_id,
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
def test_a_shape_the_scan_cannot_read_is_refused_or_allowlisted(case_id, snippet):
    """Every shape the scan cannot resolve is refused, naming the file and line, unless it
    is listed in the test's allowlist with a reason. A statement kind the walk does not
    know is refused outright. The family's own hash authority is refused by the rules
    that name it. A case in ``_REFUSAL_MARKERS`` must carry its own rule's refusal, so a
    shape refused by a neighbouring rule instead does not pass untested."""
    source = "from lionagi.models import HashableModel\nfrom pydantic import ConfigDict\n" + snippet
    try:
        scan = _sample(source)
    except ValueError as refused:
        assert "statement kind the mutable-model scan cannot read" in str(refused)
        return
    errors = _violations(scan, {*_ROOTS, "sample.Known"})
    assert errors, f"shape was neither refused nor allowlisted: {snippet!r}"
    marker = _REFUSAL_MARKERS.get(case_id)
    if marker is not None:
        assert any(marker in error for error in errors), (
            f"{case_id} must be refused by its own rule ({marker!r}), got {errors!r}"
        )


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
