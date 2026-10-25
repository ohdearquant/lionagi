# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from abc import ABC, abstractmethod

from lionagi.protocols.action.tool import Tool

__all__ = ("LionTool",)


class LionTool(ABC):
    is_lion_system_tool: bool = True
    system_tool_name: str

    @abstractmethod
    def to_tool(self) -> Tool:
        pass
