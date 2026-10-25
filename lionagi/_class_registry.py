# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import sys
import types
from typing import TypeVar

from lionagi import ln
from lionagi.ln import _utils as _ln_utils
from lionagi.ln._utils import load_type_from_string

T = TypeVar("T")
LION_CLASS_REGISTRY: dict[str, type[T]] = {}

# Built-in modules that define Element/Node subclasses.
# See docs/internals/support-libs.md#_class_registry-_builtin_modules
_BUILTIN_MODULES = (
    "lionagi.protocols.generic.element",
    "lionagi.protocols.generic.event",
    "lionagi.protocols.generic.flow",
    "lionagi.protocols.generic.log",
    "lionagi.protocols.generic.pile",
    "lionagi.protocols.generic.progression",
    "lionagi.protocols.graph.edge",
    "lionagi.protocols.graph.graph",
    "lionagi.protocols.graph.node",
    "lionagi.protocols.messages.action_request",
    "lionagi.protocols.messages.action_response",
    "lionagi.protocols.messages.assistant_response",
    "lionagi.protocols.messages.instruction",
    "lionagi.protocols.messages.message",
    "lionagi.protocols.messages.system",
)

__all__ = (
    "get_class",
    "LION_CLASS_REGISTRY",
)

_NOT_FOUND = "Unable to find class {}: {}"
# What would make the name resolve differs by its shape.
_MALFORMED = "it is not a dotted class path or a class name"
_BARE = (
    "a bare name resolves only to a class in lionagi's built-in modules or in "
    "LION_CLASS_REGISTRY; give its dotted path"
)
_MOVED = "if the class has moved, give its current dotted path"
_UNDER_PREFIX = f"no class of that name loads from that module path; {_MOVED}"
_IMPORTED = f"its module is imported and holds no class of that name; {_MOVED}"
_LAZY = (
    "its module is imported and may supply the name through a module-level "
    "__getattr__, which is called only for a module under an allowed prefix; allow "
    "its package with lionagi.ln.register_type_prefix(), giving its prefix with the "
    'trailing dot ("mypkg.")'
)
_NESTED = (
    "its path runs through a class, which a dotted path does not reach into; register "
    "the class under this dotted name in LION_CLASS_REGISTRY"
)
_OUTSIDE = (
    "its module is outside the allowed prefixes; import that module first, allow its "
    "package with lionagi.ln.register_type_prefix(), giving its prefix with the trailing "
    f'dot ("mypkg."), or, {_MOVED}'
)
# How much of a name, or of why it failed to load, an error message repeats: the
# name comes from serialized data and can be any length.
_SHOWN = 160


def _cut(text: str) -> str:
    if len(text) <= _SHOWN:
        return str.__str__(text)
    return f"{text[:_SHOWN]}... [{len(text) - _SHOWN:,} more characters]"


def _nested(class_name: str) -> bool:
    """Whether the name runs through a class held by an imported module, as the
    path of a class nested in another class does."""
    parts = class_name.split(".")
    # No imported module has more parts than the deepest name in sys.modules, so only
    # that many leading parts are tried: a name of many parts costs one pass, not one per part.
    deepest = max((name.count(".") for name in list(sys.modules)), default=0) + 1
    for end in range(min(len(parts) - 2, deepest), 0, -1):
        module = sys.modules.get(".".join(parts[:end]))
        if isinstance(module, types.ModuleType):
            # The namespace, not getattr: a module-level __getattr__ can import.
            return isinstance(vars(module).get(parts[end]), type)
    return False


def get_class(class_name: str) -> type:
    """Return the class a serialized name refers to. Raises ValueError if none
    is found, chained from the import failure when loading a dotted name failed.

    The lookup order, which modules may be imported, and the remedy each error
    gives are described once, in docs/internals/support-libs.md.
    """
    if not class_name:
        # Reject empty names here: the suffix-match fallback below would
        # otherwise match any registry key ending in a bare ".".
        raise ValueError(_NOT_FOUND.format(_quoted(class_name), _MALFORMED))

    if class_name in LION_CLASS_REGISTRY:
        return LION_CLASS_REGISTRY[class_name]

    name = class_name
    # Why loading a dotted name failed, kept for the error raised when no later
    # step resolves it either.
    load_error: Exception | None = None
    # The module prefixes a short-name match must be defined under: lionagi for
    # a bare name, narrowed below to a dotted name's own allowed prefix.
    prefixes: tuple[str, ...] = ("lionagi.",)
    if "." in class_name:
        mod, _, name = class_name.rpartition(".")
        # An empty part (".Foo", "pkg..Foo", "pkg.") names no module or class, and an
        # empty short name would match every registry key ending in ".".
        if not name or "" in mod.split("."):
            raise ValueError(_NOT_FOUND.format(_quoted(class_name), _MALFORMED))
        module = sys.modules.get(mod)
        if isinstance(module, types.ModuleType):
            # The module's own namespace, not getattr: a module-level
            # __getattr__ (PEP 562) can import further modules.
            cls = vars(module).get(name)
            if isinstance(cls, type):
                return cls
        try:
            return load_type_from_string(class_name)
        except Exception as exc:  # noqa: BLE001 — normalize import failure to lookup failure
            load_error = exc
        # Only a lionagi class (or one under a registered prefix) whose module
        # has moved falls back to its short name, and only to a class defined
        # under that same prefix; any other dotted name would resolve to
        # whichever built-in or registered class shares that name.
        allowed = _ln_utils._ALLOWED_MODULE_PREFIXES
        prefixes = tuple(p for p in allowed if class_name.startswith(p))
        if not prefixes:
            if isinstance(module, types.ModuleType):
                hint = _LAZY if "__getattr__" in vars(module) else _IMPORTED
            else:
                hint = _NESTED if _nested(class_name) else _OUTSIDE
            shown = _quoted(class_name)
            raise ValueError(_NOT_FOUND.format(shown, hint)) from load_error

    for mod_name in _BUILTIN_MODULES:
        try:
            module = ln.import_module(mod_name)
        except ImportError:
            continue
        cls = getattr(module, name, None)
        # A built-in module's namespace also holds what it imports (Path,
        # BaseModel, UUID); only a class defined under the prefix counts.
        if isinstance(cls, type) and f"{getattr(cls, '__module__', None)}.".startswith(prefixes):
            return cls

    # Importing the built-in modules above may have registered additional
    # Node subclasses under their full-qualified names; check by suffix. A
    # short name matches any of them; a dotted name only one defined under an
    # allowed prefix that the name itself starts with. The class's own module
    # decides, not the key it was registered under: a key records the module a
    # class had when it was registered, which need not be where it is defined now.
    for key, cls in LION_CLASS_REGISTRY.items():
        if key == name or key.endswith(f".{name}"):
            defined_in = getattr(cls, "__module__", None)
            if "." not in class_name or f"{defined_in}.".startswith(prefixes):
                return cls

    if "." not in class_name:
        hint = _BARE
    else:
        hint = _NESTED if _nested(class_name) else _UNDER_PREFIX
    message = _NOT_FOUND.format(_quoted(class_name), hint)
    if load_error is not None:
        # The name is under an allowed prefix, so loading it was already tried:
        # say why it failed (a module that raises on import, a missing name).
        # load_type_from_string's message repeats the whole name; the error it
        # wraps says only what failed.
        reason = _cut(str(load_error.__cause__ or load_error))
        message = f"{message} (loading it failed: {reason})"
    raise ValueError(message) from load_error


def _shown(value: object) -> str:
    """A value as an error repeats it: a string as itself, anything else by its repr,
    cut; one whose repr raises by its type's name, so the error is still raised."""
    if isinstance(value, str):
        return _cut(value)
    try:
        return _cut(repr(value))
    except Exception:
        return f"<{_cut(type(value).__name__)} object>"


def _quoted(name: object) -> str:
    """A name as an error quotes it: a string by the plain ``str`` repr, so a subclass's own
    ``__repr__`` never runs; anything else as ``_shown`` gives it."""
    return _cut(str.__repr__(name)) if isinstance(name, str) else _shown(name)
