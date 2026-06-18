# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import warnings

from .agentic_endpoint import AgenticEndpoint
from .api_calling import APICalling
from .endpoint import Endpoint
from .endpoint_config import EndpointConfig
from .header_factory import HeaderFactory
from .match_endpoint import match_endpoint
from .mcp_wrapper import MCPConnectionPool, MCPSecurityConfig, create_mcp_tool
from .provider_config import LazyType, ProviderConfig
from .registry import EndpointMeta, EndpointRegistry, EndpointType, register_endpoint

__all__ = (
    "AgenticEndpoint",
    "APICalling",
    "CLIEndpoint",
    "Endpoint",
    "EndpointConfig",
    "HeaderFactory",
    "match_endpoint",
    "MCPConnectionPool",
    "MCPSecurityConfig",
    "create_mcp_tool",
    "EndpointMeta",
    "EndpointRegistry",
    "EndpointType",
    "LazyType",
    "ProviderConfig",
    "register_endpoint",
)


def __getattr__(name: str):
    if name == "CLIEndpoint":
        warnings.warn(
            "CLIEndpoint is deprecated and will be removed in a future release. "
            "Use AgenticEndpoint from lionagi.service.connections instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Cache to prevent double-fire from importlib _handle_fromlist.
        import sys

        sys.modules[__name__].__dict__["CLIEndpoint"] = AgenticEndpoint
        return AgenticEndpoint
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
