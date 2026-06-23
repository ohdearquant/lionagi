# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
from typing import Any, ClassVar

from .base import _coerce_id_field, _unwrap_action_data
from .message import Message, MessageContent, MessageRole


@dataclass(slots=True)
class ActionResponseContent(MessageContent):
    """Content for function call results, linked back to the originating ActionRequest."""

    function: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    action_request_id: str | None = None
    error: str | None = None

    @property
    def role(self) -> MessageRole:
        return MessageRole.ACTION

    @property
    def request_id(self) -> str | None:
        """Alias for action_request_id."""
        return self.action_request_id

    @property
    def result(self) -> Any:
        """Alias for output."""
        return self.output

    @property
    def success(self) -> bool:
        """True when no error was recorded."""
        return self.error is None

    def render_summary(self) -> str:
        """Render result content for round-level aggregation."""
        from lionagi.libs.schema.minimal_yaml import minimal_yaml

        if not self.success:
            return f"error: {self.error or 'unknown'}"
        if self.output is None:
            return "ok"
        if isinstance(self.output, str):
            return self.output
        if isinstance(self.output, dict | list):
            return minimal_yaml(self.output)
        return str(self.output)

    @property
    def rendered(self) -> str:
        """Render action response as YAML."""
        from lionagi.libs.schema.minimal_yaml import minimal_yaml

        doc = {
            "Function": self.function,
            "Arguments": self.arguments,
            "Output": self.output,
        }
        return minimal_yaml(doc).strip()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionResponseContent":
        """Construct ActionResponseContent from a dict; handles legacy nested action_response key."""
        function, arguments = _unwrap_action_data(data, "action_response")

        # output and error are only in the response variant; pull from the same
        # source (inner or outer) that function/arguments came from.
        if "action_response" in data:
            inner = data["action_response"]
            output = inner.get("output")
            error = inner.get("error")
        else:
            output = data.get("output")
            error = data.get("error")

        return cls(
            function=function,
            arguments=arguments,
            output=output,
            action_request_id=_coerce_id_field(data, "action_request_id"),
            error=error,
        )


class ActionResponse(Message):
    """Message carrying the result of an executed action/function."""

    _role: ClassVar[MessageRole] = MessageRole.ACTION
    _content_type: ClassVar[type] = ActionResponseContent
    content: ActionResponseContent

    @property
    def function(self) -> str:
        return self.content.function

    @property
    def arguments(self) -> dict[str, Any]:
        return self.content.arguments

    @property
    def output(self) -> Any:
        return self.content.output
