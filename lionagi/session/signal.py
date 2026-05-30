# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Signal — a lightweight Observable envelope for the reactive bus.

A ``Signal`` carries an arbitrary payload (``data``) into the session
observer. Observers key off the *payload* type, not the Signal subclass:
``session.observe(MyModel)`` fires for any Signal whose ``data`` is a
``MyModel`` instance. The id comes for free from :class:`Element`, so the
envelope lives in a Pile/Flow like any other element.

``StructuredOutput`` is the typed case: its payload is a structured model.
It is the realization of "capabilities = structured output event" — an agent
exercises a capability by emitting a typed value; an observer reacting to
that type is the capability being honored.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..protocols.generic.element import Element
from ..protocols.messages import ActionRequest, ActionResponse

__all__ = (
    "Signal",
    "StructuredOutput",
    "ActionRequestSignal",
    "ActionResponseSignal",
)


class Signal(Element):
    """An Observable envelope carrying a payload into the reactive bus."""

    data: Any = None


class StructuredOutput(Signal):
    """A Signal whose payload is a structured (typed) model."""

    data: BaseModel


class ActionRequestSignal(Signal):
    """A tool-use emission. ``data`` is the originating ``ActionRequest``.

    Lets observers react to tool calls and track per-tool usage:
    ``session.observe(ActionRequest)`` fires for every tool invocation.
    """

    data: ActionRequest


class ActionResponseSignal(Signal):
    """A tool-result emission. ``data`` is the resolved ``ActionResponse``."""

    data: ActionResponse
