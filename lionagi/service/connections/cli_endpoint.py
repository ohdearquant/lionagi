# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

# Re-export from agentic_endpoint for backward compatibility.
from .agentic_endpoint import AgenticEndpoint as CLIEndpoint

__all__ = ("CLIEndpoint",)
