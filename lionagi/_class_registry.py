# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import importlib
import sys
from typing import TypeVar

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


def get_class(class_name: str) -> type:
    """Retrieve a class by name: LION_CLASS_REGISTRY lookup (fully-qualified
    name), then a dotted path whose module is already imported, then a dotted
    path under an allowed module prefix (``lionagi.`` and any prefix added with
    ``lionagi.ln.register_type_prefix``), then legacy short-name lookup among
    the built-in modules. A module outside the allowed prefixes is never
    imported here. Raises ValueError if not found.
    """
    if not class_name:
        # Reject empty names here: the suffix-match fallback below would
        # otherwise match any registry key ending in a bare ".".
        raise ValueError(f"Unable to find class {class_name!r}")

    if class_name in LION_CLASS_REGISTRY:
        return LION_CLASS_REGISTRY[class_name]

    name = class_name
    if "." in class_name:
        mod, _, name = class_name.rpartition(".")
        module = sys.modules.get(mod)
        if module is not None:
            cls = getattr(module, name, None)
            if isinstance(cls, type):
                return cls
        else:
            try:
                return load_type_from_string(class_name)
            except ValueError:
                pass

    for mod_name in _BUILTIN_MODULES:
        try:
            module = importlib.import_module(mod_name)
        except ImportError:
            continue
        cls = getattr(module, name, None)
        if isinstance(cls, type):
            return cls

    # Importing the built-in modules above may have registered additional
    # Node subclasses under their full-qualified names; check by suffix.
    for key, cls in LION_CLASS_REGISTRY.items():
        if key == name or key.endswith(f".{name}"):
            return cls

    raise ValueError(f"Unable to find class {class_name}")
