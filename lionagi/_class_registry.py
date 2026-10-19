# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import importlib
import sys
import types
from typing import TypeVar

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

_NOT_FOUND = (
    "Unable to find class {}: import the module that defines it first, or allow its "
    "package with lionagi.ln.register_type_prefix()"
)


def get_class(class_name: str) -> type:
    """Retrieve a class by name: LION_CLASS_REGISTRY lookup (fully-qualified
    name), then a dotted path whose module is already imported, then a dotted
    path under an allowed module prefix (``lionagi.`` and any prefix added with
    ``lionagi.ln.register_type_prefix``), then legacy short-name lookup among
    the built-in modules, then a LION_CLASS_REGISTRY key ending in the short
    name. A dotted name outside the allowed prefixes that neither of its own
    steps resolves raises instead of falling back to its short name, and one
    under an allowed prefix falls back only to a class defined under an
    allowed prefix that the name itself starts with; a bare short name resolves only to a lionagi class or a registered
    one. A module outside the allowed prefixes is never imported here. Raises
    ValueError if not found.
    """
    if not class_name:
        # Reject empty names here: the suffix-match fallback below would
        # otherwise match any registry key ending in a bare ".".
        raise ValueError(_NOT_FOUND.format(repr(class_name)))

    if class_name in LION_CLASS_REGISTRY:
        return LION_CLASS_REGISTRY[class_name]

    name = class_name
    # The module prefixes a short-name match must be defined under: lionagi for
    # a bare name, narrowed below to a dotted name's own allowed prefix.
    prefixes: tuple[str, ...] = ("lionagi.",)
    if "." in class_name:
        mod, _, name = class_name.rpartition(".")
        # An empty short name ("pkg.") would match every registry key ending in ".".
        if not mod or not name:
            raise ValueError(_NOT_FOUND.format(class_name))
        module = sys.modules.get(mod)
        if isinstance(module, types.ModuleType):
            # The module's own namespace, not getattr: a module-level
            # __getattr__ (PEP 562) can import further modules.
            cls = vars(module).get(name)
            if isinstance(cls, type):
                return cls
        try:
            return load_type_from_string(class_name)
        except Exception:  # noqa: BLE001, S110 — normalize import failure to lookup failure
            pass
        # Only a lionagi class (or one under a registered prefix) whose module
        # has moved falls back to its short name, and only to a class defined
        # under that same prefix; any other dotted name would resolve to
        # whichever built-in or registered class shares that name.
        allowed = _ln_utils._ALLOWED_MODULE_PREFIXES
        prefixes = tuple(p for p in allowed if class_name.startswith(p))
        if not prefixes:
            raise ValueError(_NOT_FOUND.format(class_name))

    for mod_name in _BUILTIN_MODULES:
        try:
            module = importlib.import_module(mod_name)
        except ImportError:
            continue
        cls = getattr(module, name, None)
        # A built-in module's namespace also holds what it imports (Path,
        # BaseModel, UUID); only a class defined under the prefix counts.
        if isinstance(cls, type) and f"{getattr(cls, '__module__', None)}.".startswith(prefixes):
            return cls

    # Importing the built-in modules above may have registered additional
    # Node subclasses under their full-qualified names; check by suffix. A
    # short name matches any of them; a dotted name only one registered under
    # an allowed prefix that the name itself starts with.
    for key, cls in LION_CLASS_REGISTRY.items():
        if key == name or key.endswith(f".{name}"):
            if "." not in class_name or key.startswith(prefixes):
                return cls

    raise ValueError(_NOT_FOUND.format(class_name))
