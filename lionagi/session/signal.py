# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Observable envelope carrying payloads into the reactive bus."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..protocols.generic.element import Element

__all__ = (
    "Signal",
    "StructuredOutput",
    "RunStart",
    "RunEnd",
    "RunFailed",
    "NodeStarted",
    "NodeCompleted",
    "NodeFailed",
    "GateDenied",
    "MessageAdded",
)


class Signal(Element):
    """Observable envelope carrying a payload into the reactive bus."""

    data: Any = None
    emitter_role: str | None = None


class StructuredOutput(Signal):
    """Signal whose payload is a structured (typed) model."""

    data: BaseModel


class RunStart(Signal):
    """Run lifecycle: beginning."""


class RunEnd(Signal):
    """Run lifecycle: completed. data is the result."""


class RunFailed(Signal):
    """Run lifecycle: raised. data is the exception."""


class NodeStarted(Signal):
    """DAG node lifecycle: began executing."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class NodeCompleted(Signal):
    """DAG node lifecycle: finished successfully."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class NodeFailed(Signal):
    """DAG node lifecycle: raised during execution."""

    op_id: str = ""
    name: str = ""
    elapsed: float = 0.0


class GateDenied(Signal):
    """Governance gate denied a proposed action."""


class MessageAdded(Signal):
    """A message was added to a branch. data is the RoledMessage."""
