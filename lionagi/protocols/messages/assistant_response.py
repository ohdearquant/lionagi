# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, field_validator

from .base import SenderRecipient
from .message import Message, MessageContent, MessageRole


def parse_assistant_response(
    response: BaseModel | list[BaseModel] | dict | str | Any,
) -> tuple[str, dict | list[dict]]:
    """Extract text and raw response from Anthropic/OpenAI/Claude Code formats; returns (text, raw)."""
    responses = [response] if not isinstance(response, list) else response

    text_contents = []
    model_responses = []

    for item in responses:
        if isinstance(item, BaseModel):
            item = item.model_dump(exclude_none=True, exclude_unset=True)

        model_responses.append(item)

        if isinstance(item, dict):
            # Anthropic standard
            if "content" in item:
                content = item["content"]
                content = [content] if not isinstance(content, list) else content
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text_contents.append(c["text"])
                    elif isinstance(c, str):
                        text_contents.append(c)

            # OpenAI chat completions standard
            elif "choices" in item:
                choices = item["choices"]
                choices = [choices] if not isinstance(choices, list) else choices
                for choice in choices:
                    if "message" in choice:
                        text_contents.append(choice["message"].get("content") or "")
                    elif "delta" in choice:
                        text_contents.append(choice["delta"].get("content") or "")

            # OpenAI responses API standard
            elif "output" in item:
                output = item["output"]
                output = [output] if not isinstance(output, list) else output
                for out in output:
                    if isinstance(out, dict) and out.get("type") == "message":
                        content = out.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "output_text":
                                    text_contents.append(c.get("text", ""))
                                elif isinstance(c, str):
                                    text_contents.append(c)

            # Claude Code standard
            elif "result" in item:
                text_contents.append(item["result"])

        elif isinstance(item, str):
            text_contents.append(item)

    text = "".join(text_contents)
    model_response = model_responses[0] if len(model_responses) == 1 else model_responses

    return text, model_response


@dataclass(slots=True)
class AssistantResponseContent(MessageContent):
    """Content for assistant responses; holds extracted text in assistant_response."""

    assistant_response: str = ""

    @property
    def role(self) -> MessageRole:
        return MessageRole.ASSISTANT

    @property
    def response(self) -> str:
        """Alias for assistant_response."""
        return self.assistant_response

    @property
    def rendered(self) -> str:
        return self.assistant_response

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssistantResponseContent":
        """Construct from dict using assistant_response key."""
        assistant_response = data.get("assistant_response", "")
        return cls(assistant_response=assistant_response)


class AssistantResponse(Message):
    """Message representing an AI assistant's reply; raw model output in metadata["model_response"]."""

    _role: ClassVar[MessageRole] = MessageRole.ASSISTANT
    content: AssistantResponseContent
    recipient: SenderRecipient | None = MessageRole.USER

    @field_validator("content", mode="before")
    def _validate_content(cls, v):
        if v is None:
            return AssistantResponseContent()
        if isinstance(v, dict):
            return AssistantResponseContent.from_dict(v)
        if isinstance(v, AssistantResponseContent):
            return v
        raise TypeError("content must be dict or AssistantResponseContent instance")

    @property
    def response(self) -> str:
        return self.content.assistant_response

    @property
    def model_response(self) -> dict | list[dict]:
        """Raw provider response stored in metadata."""
        return self.metadata.get("model_response", {})

    @classmethod
    def from_response(
        cls,
        response: BaseModel | list[BaseModel] | dict | str | Any,
        sender: SenderRecipient | None = None,
        recipient: SenderRecipient | None = None,
    ) -> "AssistantResponse":
        """Create an AssistantResponse from raw model output in any supported provider format."""
        text, model_response = parse_assistant_response(response)

        return cls(
            content=AssistantResponseContent(assistant_response=text),
            sender=sender,
            recipient=recipient or MessageRole.USER,
            metadata={"model_response": model_response},
        )
