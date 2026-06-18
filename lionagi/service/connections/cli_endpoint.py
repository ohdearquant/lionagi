# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import warnings

from .agentic_endpoint import AgenticEndpoint


def __getattr__(name: str):
    if name == "CLIEndpoint":
        warnings.warn(
            "CLIEndpoint is deprecated and will be removed in a future release. "
            "Use AgenticEndpoint from lionagi.service.connections instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return AgenticEndpoint
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
