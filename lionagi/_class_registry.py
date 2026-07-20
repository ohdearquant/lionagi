# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import importlib
from typing import TypeVar

from lionagi.ln._utils import import_module

T = TypeVar("T")
LION_CLASS_REGISTRY: dict[str, type[T]] = {}

# Built-in modules that define Element/Node subclasses. Persisted `lion_class`
# metadata written before the full-qualified-name convention was adopted
# stores a bare class name (e.g. "Instruction") instead of a dotted path.
# Importing these modules on a short-name lookup miss (a) triggers
# Node.__pydantic_init_subclass__ registration into LION_CLASS_REGISTRY for
# Node subclasses, and (b) makes every built-in class directly attribute-
# lookupable on its module, without scanning the filesystem.
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
    name), then dotted-path import, then legacy short-name lookup among the
    built-in modules. Raises ValueError if not found.
    """
    if not class_name:
        # An empty name can never be a legitimate class name; the short-name
        # suffix-match fallback below (``key.endswith(f".{name}")``) would
        # otherwise match ANY registry key ending in a bare ".", including
        # keys accidentally left by a Node subclass created with an empty
        # name (e.g. a dynamic factory called with name="").
        raise ValueError(f"Unable to find class {class_name!r}")

    if class_name in LION_CLASS_REGISTRY:
        return LION_CLASS_REGISTRY[class_name]

    name = class_name
    if "." in class_name:
        mod, _, name = class_name.rpartition(".")
        try:
            cls = import_module(mod, import_name=name)
            if isinstance(cls, type):
                return cls
        except Exception:  # noqa: BLE001, S110 — normalize import failure to lookup failure
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
