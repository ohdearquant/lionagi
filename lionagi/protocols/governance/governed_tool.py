# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""governed_tool decorator factory (P17).

Wraps a sync or async callable as a governed Tool with declarative
governance metadata stored in Tool.governance_meta.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lionagi.protocols.action.tool import Tool
from lionagi.protocols.governance.dsl import AttachLevel

__all__ = ["governed_tool"]


def governed_tool(
    *,
    permissions: list[str] | None = None,
    gate_ids: list[str] | None = None,
    evidence: AttachLevel | None = None,
    audit_classification: str | None = None,
) -> Callable[[Callable[..., Any]], Tool]:
    """Return a decorator that wraps a callable as a governed Tool.

    Only non-None values are stored in governance_meta.

    Usage::

        @governed_tool(permissions=["read:code"], gate_ids=["pii_gate"])
        async def my_tool(x: int) -> str: ...

    Returns a Tool with governance_meta set (empty dict if no kwargs given).
    """

    def decorator(fn: Callable[..., Any]) -> Tool:
        tool = Tool(func_callable=fn)
        tool.governance_meta = {
            "required_permissions": permissions if permissions is not None else [],
            "gate_ids": gate_ids if gate_ids is not None else [],
            "evidence_level": evidence.value if evidence is not None else None,
            "audit_classification": audit_classification
            if audit_classification is not None
            else "standard",
        }
        return tool

    return decorator
