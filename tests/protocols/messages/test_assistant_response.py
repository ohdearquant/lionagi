# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest
from pydantic import BaseModel

from lionagi.protocols.messages.assistant_response import (
    AssistantResponse,
    AssistantResponseContent,
    parse_assistant_response,
)
from lionagi.protocols.types import MessageRole

# ============================================================================
# Mock Response Models for Testing
# ============================================================================


class AnthropicTextContent(BaseModel):
    """Mock Anthropic text content block"""

    type: str = "text"
    text: str


class AnthropicResponse(BaseModel):
    """Mock Anthropic API response"""

    content: list[AnthropicTextContent]
    model: str = "claude-3-opus"


class OpenAIMessage(BaseModel):
    """Mock OpenAI message"""

    content: str | None = None


class OpenAIDelta(BaseModel):
    """Mock OpenAI delta for streaming"""

    content: str | None = None


class OpenAIChoice(BaseModel):
    """Mock OpenAI choice"""

    message: OpenAIMessage | None = None
    delta: OpenAIDelta | None = None


class OpenAIChatResponse(BaseModel):
    """Mock OpenAI chat completion response"""

    choices: list[OpenAIChoice]
    model: str = "gpt-4"


class OpenAIOutputText(BaseModel):
    """Mock OpenAI responses API output text"""

    type: str = "output_text"
    text: str


class OpenAIOutputMessage(BaseModel):
    """Mock OpenAI responses API message"""

    type: str = "message"
    content: list[OpenAIOutputText]


class OpenAIResponsesAPIResponse(BaseModel):
    """Mock OpenAI responses API response"""

    output: list[OpenAIOutputMessage]


class ClaudeCodeResponse(BaseModel):
    """Mock Claude Code response"""

    result: str


# ============================================================================
# Test parse_assistant_response() Function
# ============================================================================


def test_parse_assistant_response_anthropic_format():
    response = AnthropicResponse(
        content=[
            AnthropicTextContent(type="text", text="Hello "),
            AnthropicTextContent(type="text", text="world!"),
        ]
    )

    text, model_response = parse_assistant_response(response)

    assert text == "Hello world!"
    assert isinstance(model_response, dict)
    assert "content" in model_response


def test_parse_assistant_response_anthropic_dict():
    response = {
        "content": [
            {"type": "text", "text": "First part "},
            {"type": "text", "text": "second part"},
        ]
    }

    text, model_response = parse_assistant_response(response)

    assert text == "First part second part"
    assert model_response == response


def test_parse_assistant_response_anthropic_string_content():
    response = {"content": "Simple string content"}

    text, model_response = parse_assistant_response(response)

    assert text == "Simple string content"


def test_parse_assistant_response_openai_chat_format():
    response = OpenAIChatResponse(
        choices=[
            OpenAIChoice(message=OpenAIMessage(content="Response 1")),
            OpenAIChoice(message=OpenAIMessage(content="Response 2")),
        ]
    )

    text, model_response = parse_assistant_response(response)

    assert text == "Response 1Response 2"
    assert isinstance(model_response, dict)
    assert "choices" in model_response


def test_parse_assistant_response_openai_streaming():
    response = OpenAIChatResponse(
        choices=[
            OpenAIChoice(delta=OpenAIDelta(content="Hello ")),
        ]
    )

    text, model_response = parse_assistant_response(response)

    assert text == "Hello "


def test_parse_assistant_response_openai_responses_api():
    response = OpenAIResponsesAPIResponse(
        output=[
            OpenAIOutputMessage(
                type="message",
                content=[
                    OpenAIOutputText(type="output_text", text="Part 1 "),
                    OpenAIOutputText(type="output_text", text="Part 2"),
                ],
            )
        ]
    )

    text, model_response = parse_assistant_response(response)

    assert text == "Part 1 Part 2"
    assert isinstance(model_response, dict)
    assert "output" in model_response


def test_parse_assistant_response_claude_code_format():
    response = ClaudeCodeResponse(result="Claude Code response")

    text, model_response = parse_assistant_response(response)

    assert text == "Claude Code response"
    assert isinstance(model_response, dict)
    assert "result" in model_response


def test_parse_assistant_response_raw_string():
    response = "Simple text response"

    text, model_response = parse_assistant_response(response)

    assert text == "Simple text response"
    assert model_response == "Simple text response"


def test_parse_assistant_response_list_of_responses():
    responses = [
        {"content": [{"type": "text", "text": "First"}]},
        {"content": [{"type": "text", "text": " Second"}]},
    ]

    text, model_response = parse_assistant_response(responses)

    assert text == "First Second"
    assert isinstance(model_response, list)
    assert len(model_response) == 2


def test_parse_assistant_response_list_of_strings():
    responses = ["Hello ", "world", "!"]

    text, model_response = parse_assistant_response(responses)

    assert text == "Hello world!"
    assert isinstance(model_response, list)
    assert len(model_response) == 3


def test_parse_assistant_response_empty_content():
    response = OpenAIChatResponse(choices=[OpenAIChoice(message=OpenAIMessage(content=None))])

    text, model_response = parse_assistant_response(response)

    assert text == ""


def test_parse_assistant_response_mixed_content_types():
    response = {
        "content": [
            {"type": "text", "text": "Text block"},
            {"type": "image", "data": "base64..."},  # Should be ignored
            "Plain string",
        ]
    }

    text, model_response = parse_assistant_response(response)

    assert text == "Text blockPlain string"


# ============================================================================
# Test AssistantResponseContent Dataclass
# ============================================================================


def test_assistant_response_content_initialization():
    content = AssistantResponseContent(assistant_response="Test content")

    assert content.assistant_response == "Test content"
    assert hasattr(content, "__slots__")  # Verify slots=True


def test_assistant_response_content_default():
    content = AssistantResponseContent()

    assert content.assistant_response == ""


def test_assistant_response_content_rendered_property():
    content = AssistantResponseContent(assistant_response="Rendered text")

    assert content.rendered == "Rendered text"


def test_assistant_response_content_from_dict():
    data = {"assistant_response": "From dict"}
    content = AssistantResponseContent.from_dict(data)

    assert isinstance(content, AssistantResponseContent)
    assert content.assistant_response == "From dict"


def test_assistant_response_content_from_dict_empty():
    data = {}
    content = AssistantResponseContent.from_dict(data)

    assert content.assistant_response == ""


# ============================================================================
# Test AssistantResponse Message Class
# ============================================================================


def test_assistant_response_initialization():
    content = AssistantResponseContent(assistant_response="Test response")
    response = AssistantResponse(content=content)

    assert response.role == MessageRole.ASSISTANT
    assert response.content.assistant_response == "Test response"
    assert response.recipient == MessageRole.USER


def test_assistant_response_initialization_with_dict_content():
    response = AssistantResponse(content={"assistant_response": "Dict content"})

    assert isinstance(response.content, AssistantResponseContent)
    assert response.content.assistant_response == "Dict content"


def test_assistant_response_initialization_with_none_content():
    response = AssistantResponse(content=None)

    assert isinstance(response.content, AssistantResponseContent)
    assert response.content.assistant_response == ""


def test_assistant_response_content_validation_invalid_type():
    with pytest.raises(TypeError, match="content must be dict or AssistantResponseContent"):
        AssistantResponse(content="invalid string")


def test_assistant_response_sender_recipient():
    content = AssistantResponseContent(assistant_response="Test")
    response = AssistantResponse(
        content=content,
        sender="assistant",
        recipient="user",
    )

    assert response.sender == MessageRole.ASSISTANT
    assert response.recipient == MessageRole.USER


# ============================================================================
# Test from_response() Classmethod
# ============================================================================


def test_from_response_anthropic():
    response = AnthropicResponse(
        content=[AnthropicTextContent(type="text", text="Anthropic response")]
    )

    assistant_response = AssistantResponse.from_response(response)

    assert assistant_response.content.assistant_response == "Anthropic response"
    assert "model_response" in assistant_response.metadata
    assert isinstance(assistant_response.model_response, dict)


def test_from_response_openai_chat():
    response = OpenAIChatResponse(
        choices=[OpenAIChoice(message=OpenAIMessage(content="OpenAI response"))]
    )

    assistant_response = AssistantResponse.from_response(
        response,
        sender="assistant",
        recipient="user",
    )

    assert assistant_response.content.assistant_response == "OpenAI response"
    assert assistant_response.sender == MessageRole.ASSISTANT
    assert assistant_response.recipient == MessageRole.USER


def test_from_response_raw_string():
    assistant_response = AssistantResponse.from_response("Plain text response")

    assert assistant_response.content.assistant_response == "Plain text response"
    assert assistant_response.model_response == "Plain text response"


def test_from_response_list_of_responses():
    responses = [
        {"content": [{"type": "text", "text": "First "}]},
        {"content": [{"type": "text", "text": "Second"}]},
    ]

    assistant_response = AssistantResponse.from_response(responses)

    assert assistant_response.content.assistant_response == "First Second"
    assert isinstance(assistant_response.model_response, list)


def test_from_response_default_recipient():
    response = "Test"
    assistant_response = AssistantResponse.from_response(response)

    assert assistant_response.recipient == MessageRole.USER


def test_from_response_custom_recipient():
    response = "Test"
    assistant_response = AssistantResponse.from_response(response, recipient="system")

    assert assistant_response.recipient == MessageRole.SYSTEM


def test_from_response_preserves_all_formats():
    test_cases = [
        (
            ClaudeCodeResponse(result="Claude response"),
            "Claude response",
        ),
        (
            {"result": "Dict with result"},
            "Dict with result",
        ),
        (
            OpenAIResponsesAPIResponse(
                output=[
                    OpenAIOutputMessage(
                        type="message",
                        content=[OpenAIOutputText(type="output_text", text="Responses API")],
                    )
                ]
            ),
            "Responses API",
        ),
    ]

    for response, expected_text in test_cases:
        assistant_response = AssistantResponse.from_response(response)
        assert assistant_response.content.assistant_response == expected_text


# ============================================================================
# Test model_response Property
# ============================================================================


def test_model_response_property_access():
    model_data = {"choices": [{"message": {"content": "Test"}}]}
    response = AssistantResponse(
        content=AssistantResponseContent(assistant_response="Test"),
        metadata={"model_response": model_data},
    )

    assert response.model_response == model_data


def test_model_response_property_empty():
    response = AssistantResponse(content=AssistantResponseContent(assistant_response="Test"))

    assert response.model_response == {}


def test_model_response_property_from_response():
    original_response = OpenAIChatResponse(
        choices=[OpenAIChoice(message=OpenAIMessage(content="Test"))],
        model="gpt-4",
    )

    assistant_response = AssistantResponse.from_response(original_response)

    model_response = assistant_response.model_response
    assert isinstance(model_response, dict)
    assert "choices" in model_response
    assert model_response["model"] == "gpt-4"


# ============================================================================
# Test Integration and Edge Cases
# ============================================================================


def test_assistant_response_complete_workflow():
    # Simulate real API response
    api_response = AnthropicResponse(
        content=[
            AnthropicTextContent(type="text", text="The answer is "),
            AnthropicTextContent(type="text", text="42."),
        ]
    )

    # Create AssistantResponse
    response = AssistantResponse.from_response(api_response, sender="assistant", recipient="user")

    # Verify all properties
    assert response.role == MessageRole.ASSISTANT
    assert response.content.assistant_response == "The answer is 42."
    assert response.sender == MessageRole.ASSISTANT
    assert response.recipient == MessageRole.USER
    assert "content" in response.model_response


def test_assistant_response_streaming_simulation():
    # Simulate stream chunks
    chunks = [
        OpenAIChatResponse(choices=[OpenAIChoice(delta=OpenAIDelta(content="Hello"))]),
        OpenAIChatResponse(choices=[OpenAIChoice(delta=OpenAIDelta(content=" "))]),
        OpenAIChatResponse(choices=[OpenAIChoice(delta=OpenAIDelta(content="world"))]),
    ]

    response = AssistantResponse.from_response(chunks)

    assert response.content.assistant_response == "Hello world"
    assert isinstance(response.model_response, list)
    assert len(response.model_response) == 3


def test_assistant_response_empty_response():
    response = AssistantResponse.from_response("")

    assert response.content.assistant_response == ""
    assert response.model_response == ""


def test_assistant_response_content_rendering():
    response = AssistantResponse.from_response("Test content for rendering")

    assert response.content.rendered == "Test content for rendering"


def test_assistant_response_role_immutable():
    response = AssistantResponse(content=AssistantResponseContent(assistant_response="Test"))

    assert response.role == MessageRole.ASSISTANT
    # Role should be set at class level


def test_parse_assistant_response_none_values():
    response = {"content": None}

    text, model_response = parse_assistant_response(response)

    # Should not crash, returns empty string
    assert text == ""


def test_parse_assistant_response_malformed_structure():
    response = {"unexpected_field": "value"}

    text, model_response = parse_assistant_response(response)

    # Should handle gracefully
    assert text == ""
    assert model_response == response


def test_assistant_response_dataclass_slots():
    content = AssistantResponseContent(assistant_response="Test")

    # Verify slots behavior - should not be able to add arbitrary attributes
    with pytest.raises(AttributeError):
        content.new_attribute = "should fail"


def test_from_response_with_complex_nested_structure():
    complex_response = {
        "content": [
            {"type": "text", "text": "Part 1"},
            {"type": "text", "text": " Part 2"},
            {"type": "text", "text": " Part 3"},
        ],
        "metadata": {"model": "test", "usage": {"tokens": 100}},
    }

    assistant_response = AssistantResponse.from_response(complex_response)

    assert assistant_response.content.assistant_response == "Part 1 Part 2 Part 3"
    assert "content" in assistant_response.model_response
    assert "metadata" in assistant_response.model_response


def test_model_response_is_readonly_through_property():
    response = AssistantResponse.from_response("Test")

    model_response = response.model_response
    # Modifying returned value shouldn't affect original
    if isinstance(model_response, dict):
        model_response["new_key"] = "new_value"
        # Original should still be accessible
        assert "model_response" in response.metadata
