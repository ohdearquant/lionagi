# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from lionagi.ln import copy
from lionagi.utils import to_dict

from .base import _coerce_id_field, _unwrap_action_data
from .message import Message, MessageContent, MessageRole


@dataclass(slots=True)
class ActionRequestContent(MessageContent):
    """Content for LLM-emitted function call requests."""

    function: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    action_response_id: str | None = None

    @property
    def rendered(self) -> str:
        """Render action request as YAML."""
        from lionagi.libs.schema.minimal_yaml import minimal_yaml

        doc = {
            "Function": self.function,
            "Arguments": self.arguments,
        }
        return minimal_yaml(doc).strip()

    def render_compact(self) -> str:
        """Function-call representation for round summaries."""
        func = self.function or "unknown"
        parts = [
            f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in self.arguments.items()
        ]
        return f"{func}({', '.join(parts)})"

    @property
    def role(self) -> MessageRole:
        return MessageRole.ACTION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionRequestContent":
        """Construct ActionRequestContent from a dict; handles legacy nested action_request key."""
        function, arguments = _unwrap_action_data(data, "action_request")

        # Handle callable
        if isinstance(function, Callable):
            function = function.__name__
        if hasattr(function, "function"):
            function = function.function
        if not isinstance(function, str):
            raise ValueError("Function must be a string or callable")

        # Normalize arguments
        arguments = copy(arguments)
        if not isinstance(arguments, dict):
            try:
                arguments = to_dict(arguments, fuzzy_parse=True)
                if isinstance(arguments, list | tuple) and len(arguments) > 0:
                    arguments = arguments[0]
            except Exception:
                raise ValueError("Arguments must be a dictionary") from None

        return cls(
            function=function,
            arguments=arguments,
            action_response_id=_coerce_id_field(data, "action_response_id"),
        )


class ActionRequest(Message):
    """Message requesting an action or function execution."""

    _role: ClassVar[MessageRole] = MessageRole.ACTION
    _content_type: ClassVar[type] = ActionRequestContent
    content: ActionRequestContent

    @property
    def function(self) -> str:
        return self.content.function

    @property
    def arguments(self) -> dict[str, Any]:
        return self.content.arguments

    def is_responded(self) -> bool:
        """True if a corresponding ActionResponse has been linked."""
        return self.content.action_response_id is not None
