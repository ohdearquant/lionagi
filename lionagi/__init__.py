# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import TYPE_CHECKING

from . import ln as ln
from .ln._lazy_init import lazy_import
from .ln.types import DataClass, Operable, Params, Spec, Undefined, Unset
from .version import __version__

if TYPE_CHECKING:
    from pydantic import BaseModel, Field

    from . import _types as types
    from .adapters import (
        Adaptable,
        AdapterError,
        AdapterRegistry,
        AsyncAdaptable,
        AsyncAdapterRegistry,
        CsvAdapter,
        JsonAdapter,
        TomlAdapter,
    )
    from .ln import alcall, json_dumps, lcall, to_dict, to_list
    from .lndl import (
        InvalidConstructorError,
        LNDLError,
        LNDLOutput,
        MissingFieldError,
        MissingLvarError,
        TypeMismatchError,
        extract_lndl_blocks,
        get_lndl_system_prompt,
        normalize_lndl_text,
    )
    from .models.field_model import FieldModel
    from .models.operable_model import OperableModel
    from .operations.builder import OperationGraphBuilder as Builder
    from .operations.node import Operation
    from .protocols.action.manager import load_mcp_tools
    from .protocols.messages import Message, create_message
    from .protocols.types import Edge, Element, Event, Graph, Node, Pile, Progression
    from .service.broadcaster import Broadcaster
    from .service.hooks import HookedEvent, HookRegistry
    from .service.imodel import iModel
    from .session.branch import Branch
    from .session.session import Session

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_LAZY_MAP: dict[str, tuple[str, str | None]] = {
    "Session": ("session.session", "Session"),
    "Branch": ("session.branch", "Branch"),
    "iModel": ("service.imodel", "iModel"),
    "Builder": ("operations.builder", "OperationGraphBuilder"),
    "Operation": ("operations.node", "Operation"),
    "load_mcp_tools": ("protocols.action.manager", "load_mcp_tools"),
    "FieldModel": ("models.field_model", "FieldModel"),
    "OperableModel": ("models.operable_model", "OperableModel"),
    "Element": ("protocols.generic.element", "Element"),
    "Pile": ("protocols.generic.pile", "Pile"),
    "Progression": ("protocols.generic.progression", "Progression"),
    "Node": ("protocols.graph.node", "Node"),
    "Edge": ("protocols.graph.edge", "Edge"),
    "Graph": ("protocols.graph.graph", "Graph"),
    "Event": ("protocols.generic.event", "Event"),
    "Message": ("protocols.messages", "Message"),
    "create_message": ("protocols.messages.manager", "create_message"),
    "HookRegistry": ("service.hooks.hook_registry", "HookRegistry"),
    "HookedEvent": ("service.hooks.hooked_event", "HookedEvent"),
    "Broadcaster": ("service.broadcaster", "Broadcaster"),
    "alcall": ("ln", "alcall"),
    "lcall": ("ln", "lcall"),
    "to_list": ("ln", "to_list"),
    "to_dict": ("ln", "to_dict"),
    "json_dumps": ("ln", "json_dumps"),
    # lndl — Lion Notation Definition Language
    "LNDLOutput": ("lndl", "LNDLOutput"),
    "LNDLError": ("lndl", "LNDLError"),
    "MissingLvarError": ("lndl", "MissingLvarError"),
    "MissingFieldError": ("lndl", "MissingFieldError"),
    "TypeMismatchError": ("lndl", "TypeMismatchError"),
    "InvalidConstructorError": ("lndl", "InvalidConstructorError"),
    "get_lndl_system_prompt": ("lndl", "get_lndl_system_prompt"),
    "extract_lndl_blocks": ("lndl", "extract_lndl_blocks"),
    "normalize_lndl_text": ("lndl", "normalize_lndl_text"),
    # adapters — inlined adapter stack
    "AdapterRegistry": ("adapters", "AdapterRegistry"),
    "AsyncAdapterRegistry": ("adapters", "AsyncAdapterRegistry"),
    "Adaptable": ("adapters", "Adaptable"),
    "AsyncAdaptable": ("adapters", "AsyncAdaptable"),
    "AdapterError": ("adapters", "AdapterError"),
    "JsonAdapter": ("adapters", "JsonAdapter"),
    "CsvAdapter": ("adapters", "CsvAdapter"),
    "TomlAdapter": ("adapters", "TomlAdapter"),
}


def __getattr__(name: str):
    if name in ("BaseModel", "Field"):
        from pydantic import BaseModel, Field

        globals()["BaseModel"] = BaseModel
        globals()["Field"] = Field
        return BaseModel if name == "BaseModel" else Field
    if name == "types":
        from . import _types as types

        globals()["types"] = types
        return types
    return lazy_import(name, _LAZY_MAP, __name__, globals())


__all__ = (
    "__version__",
    "Adaptable",
    "AdapterError",
    "AdapterRegistry",
    "AsyncAdaptable",
    "AsyncAdapterRegistry",
    "BaseModel",
    "Branch",
    "Broadcaster",
    "Builder",
    "CsvAdapter",
    "DataClass",
    "Edge",
    "Element",
    "Event",
    "Field",
    "FieldModel",
    "Graph",
    "HookRegistry",
    "HookedEvent",
    "InvalidConstructorError",
    "JsonAdapter",
    "LNDLError",
    "LNDLOutput",
    "Message",
    "MissingFieldError",
    "MissingLvarError",
    "Node",
    "Operable",
    "OperableModel",
    "Operation",
    "Params",
    "Pile",
    "Progression",
    "Session",
    "Spec",
    "TomlAdapter",
    "TypeMismatchError",
    "Undefined",
    "Unset",
    "alcall",
    "create_message",
    "extract_lndl_blocks",
    "get_lndl_system_prompt",
    "iModel",
    "json_dumps",
    "lcall",
    "ln",
    "load_mcp_tools",
    "logger",
    "normalize_lndl_text",
    "to_dict",
    "to_list",
    "types",
)
