"""
OpenAI Model Names extracted from generated models.

This module provides lists of allowed model names for different OpenAI services,
extracted from the auto-generated openai_models.py file.
"""

from __future__ import annotations

import warnings
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")


# Manually define the chat models from the ChatModel class in openai_models.py
# These are extracted from the Literal type definition
CHAT_MODELS = (
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-2025-08-07",
    "gpt-5-mini-2025-08-07",
    "gpt-5-nano-2025-08-07",
    "gpt-5-chat-latest",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano-2025-04-14",
    "o4-mini",
    "o4-mini-2025-04-16",
    "o3",
    "o3-2025-04-16",
    "o3-mini",
    "o3-mini-2025-01-31",
    "o1",
    "o1-2024-12-17",
    "o1-preview",
    "o1-preview-2024-09-12",
    "o1-mini",
    "o1-mini-2024-09-12",
    "gpt-4o",
    "gpt-4o-2024-11-20",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-05-13",
    "gpt-4o-audio-preview",
    "gpt-4o-audio-preview-2024-10-01",
    "gpt-4o-audio-preview-2024-12-17",
    "gpt-4o-audio-preview-2025-06-03",
    "gpt-4o-mini-audio-preview",
    "gpt-4o-mini-audio-preview-2024-12-17",
    "gpt-4o-search-preview",
    "gpt-4o-mini-search-preview",
    "gpt-4o-search-preview-2025-03-11",
    "gpt-4o-mini-search-preview-2025-03-11",
    "chatgpt-4o-latest",
    "codex-mini-latest",
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
    "gpt-4-turbo",
    "gpt-4-turbo-2024-04-09",
    "gpt-4-0125-preview",
    "gpt-4-turbo-preview",
    "gpt-4-1106-preview",
    "gpt-4-vision-preview",
    "gpt-4",
    "gpt-4-0314",
    "gpt-4-0613",
    "gpt-4-32k",
    "gpt-4-32k-0314",
    "gpt-4-32k-0613",
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-16k",
    "gpt-3.5-turbo-0301",
    "gpt-3.5-turbo-0613",
    "gpt-3.5-turbo-1106",
    "gpt-3.5-turbo-0125",
    "gpt-3.5-turbo-16k-0613",
    "o1-pro",
    "o1-pro-2025-03-19",
    "o3-pro",
    "o3-pro-2025-06-10",
)

REASONING_MODELS = (
    model
    for model in CHAT_MODELS
    if model.startswith(("o1", "o1-", "o3", "o3-", "o4", "o4-", "gpt-5"))
)

# Embedding models
EMBEDDING_MODELS = (
    "text-embedding-ada-002",
    "text-embedding-3-small",
    "text-embedding-3-large",
)

IMAGE_MODELS = ("dall-e-2", "dall-e-3", "gpt-image-1")

MODERATION_MODELS = ("text-moderation-latest", "text-moderation-stable")


ChatModels = Literal[CHAT_MODELS]
ReasoningModels = Literal[REASONING_MODELS]
EmbeddingModels = Literal[EMBEDDING_MODELS]
ImageModels = Literal[IMAGE_MODELS]
ModerationModels = Literal[MODERATION_MODELS]


# Audio models
AUDIO_MODELS = {
    "tts": ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"],
    "transcription": [
        "whisper-1",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
    ],
}


# ---------- Roles & content parts ----------


class ChatRole(str, Enum):
    system = "system"
    developer = "developer"  # modern system-like role
    user = "user"
    assistant = "assistant"
    tool = "tool"  # for tool results sent back to the model


class TextPart(BaseModel):
    """Text content part for multimodal messages."""

    type: Literal["text"] = "text"
    text: str


class ImageURLObject(BaseModel):
    """Image URL object; 'detail' is optional and model-dependent."""

    url: str
    detail: Literal["auto", "low", "high"] | None = Field(
        default=None,
        description="Optional detail control for vision models (auto/low/high).",
    )


class ImageURLPart(BaseModel):
    """Image content part for multimodal messages."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageURLObject


ContentPart = TextPart | ImageURLPart


# ---------- Tool-calling structures ----------


class FunctionDef(BaseModel):
    """JSON Schema function definition for tool-calling."""

    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing function parameters.",
    )


class FunctionTool(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDef


class FunctionCall(BaseModel):
    """Legacy function_call field on assistant messages."""

    name: str
    arguments: str


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    """Assistant's tool call (modern)."""

    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class ToolChoiceFunction(BaseModel):
    """Explicit tool selection."""

    type: Literal["function"] = "function"
    function: dict[str, str]  # {"name": "<function_name>"}


ToolChoice = Union[Literal["auto", "none"], ToolChoiceFunction]


# ---------- Response format (structured outputs) ----------


class ResponseFormatText(BaseModel):
    type: Literal["text"] = "text"


class ResponseFormatJSONObject(BaseModel):
    type: Literal["json_object"] = "json_object"


class JSONSchemaFormat(BaseModel):
    name: str
    schema_: dict[str, Any] = Field(alias="schema", description="JSON Schema definition")
    strict: bool | None = Field(
        default=None,
        description="If true, disallow unspecified properties (strict schema).",
    )

    model_config = {"populate_by_name": True}


class ResponseFormatJSONSchema(BaseModel):
    type: Literal["json_schema"] = "json_schema"
    json_schema: JSONSchemaFormat


ResponseFormat = Union[
    ResponseFormatText,
    ResponseFormatJSONObject,
    ResponseFormatJSONSchema,
]


# ---------- Messages (discriminated by role) ----------


class SystemMessage(BaseModel):
    role: Literal[ChatRole.system] = ChatRole.system
    content: str | list[ContentPart]
    name: str | None = None  # optional per API


class DeveloperMessage(BaseModel):
    role: Literal[ChatRole.developer] = ChatRole.developer
    content: str | list[ContentPart]
    name: str | None = None


class UserMessage(BaseModel):
    role: Literal[ChatRole.user] = ChatRole.user
    content: str | list[ContentPart]
    name: str | None = None


class AssistantMessage(BaseModel):
    role: Literal[ChatRole.assistant] = ChatRole.assistant
    # Either textual content, or only tool_calls (when asking you to call tools)
    content: str | list[ContentPart] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None  # modern tool-calling result
    function_call: FunctionCall | None = None  # legacy function-calling result


class ToolMessage(BaseModel):
    role: Literal[ChatRole.tool] = ChatRole.tool
    content: str  # tool output returned to the model
    tool_call_id: str  # must reference the assistant's tool_calls[i].id


ChatMessage = SystemMessage | DeveloperMessage | UserMessage | AssistantMessage | ToolMessage

# ---------- Stream options ----------


class StreamOptions(BaseModel):
    include_usage: bool | None = Field(
        default=None,
        description="If true, a final streamed chunk includes token usage.",
    )


# ---------- Main request model ----------


class OpenAIChatCompletionsRequest(BaseModel):
    """
    Request body for OpenAI Chat Completions.
    Endpoint: POST https://api.openai.com/v1/chat/completions
    """

    # Required
    model: str = Field(..., description="Model name, e.g., 'gpt-4o', 'gpt-4o-mini'.")  # type: ignore
    messages: list[ChatMessage] = Field(
        ...,
        description="Conversation so far, including system/developer context.",
    )

    # Sampling & penalties
    temperature: float | None = Field(
        default=None, ge=0.0, le=2.0, description="Higher is more random."
    )
    top_p: float | None = Field(default=None, ge=0.0, le=1.0, description="Nucleus sampling.")
    presence_penalty: float | None = Field(
        default=None,
        ge=-2.0,
        le=2.0,
        description="Encourages new topics; -2..2.",
    )
    frequency_penalty: float | None = Field(
        default=None,
        ge=-2.0,
        le=2.0,
        description="Penalizes repetition; -2..2.",
    )

    # Token limits
    max_completion_tokens: int | None = Field(
        default=None,
        description="Preferred cap on generated tokens (newer models).",
    )
    max_tokens: int | None = Field(
        default=None,
        description="Legacy completion cap (still accepted by many models).",
    )

    # Count, stop, logits
    n: int | None = Field(default=None, ge=1, description="# of choices to generate.")
    stop: str | list[str] | None = Field(default=None, description="Stop sequence(s).")
    logit_bias: dict[str, float] | None = Field(
        default=None,
        description="Map of token-id -> bias (-100..100).",
    )
    seed: int | None = Field(
        default=None,
        description="Optional reproducibility seed (model-dependent).",
    )
    logprobs: bool | None = None
    top_logprobs: int | None = Field(
        default=None,
        ge=0,
        description="When logprobs is true, how many top tokens to include.",
    )

    # Tool calling (modern)
    tools: list[FunctionTool] | None = None
    tool_choice: ToolChoice | None = Field(
        default=None,
        description="'auto' (default), 'none', or a function selection.",
    )
    parallel_tool_calls: bool | None = Field(
        default=None,
        description="Allow multiple tool calls in a single assistant turn.",
    )

    # Legacy function-calling (still supported)
    functions: list[FunctionDef] | None = None
    function_call: Literal["none", "auto"] | FunctionCall | None = None

    # Structured outputs
    response_format: ResponseFormat | None = None

    # Streaming
    stream: bool | None = None
    stream_options: StreamOptions | None = None

    # Routing / tiering
    service_tier: Literal["auto", "default", "flex", "scale", "priority"] | None = Field(
        default=None,
        description="Processing tier; requires account eligibility.",
    )

    # Misc
    user: str | None = Field(
        default=None,
        description="End-user identifier for abuse monitoring & analytics.",
    )
    store: bool | None = Field(
        default=None,
        description="Whether to store the response server-side (model-dependent).",
    )
    metadata: dict[str, Any] | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="For reasoning models: trade-off between speed and accuracy.",
    )

    @model_validator(mode="after")
    def _validate_reasoning_model_params(self):
        if self.is_openai_model:
            if self.is_reasoning_model:
                self.temperature = None
                self.top_p = None
                self.logprobs = None
                self.top_logprobs = None
                self.logit_bias = None
            else:
                self.reasoning_effort = None
        return self

    @property
    def is_reasoning_model(self) -> bool:
        return self.model in REASONING_MODELS

    @property
    def is_openai_model(self) -> bool:
        return self.model in CHAT_MODELS
