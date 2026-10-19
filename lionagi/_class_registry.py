# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import importlib
import sys
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


def get_class(class_name: str) -> type:
    """Retrieve a class by name: LION_CLASS_REGISTRY lookup (fully-qualified
    name), then a dotted path whose module is already imported, then a dotted
    path under an allowed module prefix (``lionagi.`` and any prefix added with
    ``lionagi.ln.register_type_prefix``), then legacy short-name lookup among
    the built-in modules, then a LION_CLASS_REGISTRY key ending in the short
    name. A dotted name outside the allowed prefixes that neither of its own
    steps resolves raises instead of falling back to its short name. A module
    outside the allowed prefixes is never imported here. Raises ValueError if
    not found.
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
        # has moved falls back to its short name; any other dotted name would
        # resolve to whichever built-in or registered class shares that name.
        allowed = _ln_utils._ALLOWED_MODULE_PREFIXES
        if not name or not any(class_name.startswith(p) for p in allowed):
            raise ValueError(f"Unable to find class {class_name}")

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
