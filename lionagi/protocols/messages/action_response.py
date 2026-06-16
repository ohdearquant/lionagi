# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import field_validator

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
        # Handle nested structure from old format
        if "action_response" in data:
            resp = data["action_response"]
            function = resp.get("function", "")
            arguments = resp.get("arguments", {})
            output = resp.get("output")
        else:
            function = data.get("function", "")
            arguments = data.get("arguments", {})
            output = data.get("output")

        action_request_id = data.get("action_request_id")
        if action_request_id:
            action_request_id = str(action_request_id)

        return cls(
            function=function,
            arguments=arguments,
            output=output,
            action_request_id=action_request_id,
        )


class ActionResponse(Message):
    """Message carrying the result of an executed action/function."""

    _role: ClassVar[MessageRole] = MessageRole.ACTION
    content: ActionResponseContent

    @field_validator("content", mode="before")
    def _validate_content(cls, v):
        if v is None:
            return ActionResponseContent()
        if isinstance(v, dict):
            return ActionResponseContent.from_dict(v)
        if isinstance(v, ActionResponseContent):
            return v
        raise TypeError("content must be dict or ActionResponseContent instance")

    @property
    def function(self) -> str:
        return self.content.function

    @property
    def arguments(self) -> dict[str, Any]:
        return self.content.arguments

    @property
    def output(self) -> Any:
        return self.content.output
