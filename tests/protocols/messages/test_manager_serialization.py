import pytest
from pydantic import BaseModel

from lionagi.protocols.types import (
    Instruction,
    MessageManager,
)


class RequestModel(BaseModel):
    """Model for testing request fields"""

    name: str
    age: int


@pytest.fixture
def message_manager():
    """Fixture providing a clean MessageManager instance"""
    return MessageManager()


"""Tests for MessageManager serialization, chat_msgs, and complex flows."""


def test_to_chat_msgs_basic(message_manager):
    """Test conversion to chat messages"""
    message_manager.add_message(
        instruction="Test instruction", sender="user", recipient="assistant"
    )
    message_manager.add_message(
        assistant_response="Test response",
        sender="assistant",
        recipient="user",
    )

    chat_msgs = message_manager.to_chat_msgs()
    assert len(chat_msgs) == 2
    assert all(isinstance(msg, dict) for msg in chat_msgs)
    assert all("role" in msg and "content" in msg for msg in chat_msgs)


def test_to_chat_msgs_with_progression(message_manager):
    """Test conversion to chat messages with specific progression"""
    msg1 = message_manager.add_message(instruction="First", sender="user", recipient="assistant")
    msg2 = message_manager.add_message(
        assistant_response="Second", sender="assistant", recipient="user"
    )
    msg3 = message_manager.add_message(instruction="Third", sender="user", recipient="assistant")

    # Get only first two messages
    chat_msgs = message_manager.to_chat_msgs(progression=[msg1.id, msg2.id])
    assert len(chat_msgs) == 2


def test_to_chat_msgs_empty_progression(message_manager):
    """Test conversion with empty progression"""
    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")

    chat_msgs = message_manager.to_chat_msgs(progression=[])
    assert chat_msgs == []


def test_to_chat_msgs_invalid_progression(message_manager):
    """Test conversion with invalid progression raises error"""
    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")

    with pytest.raises(ValueError, match="invalid"):
        message_manager.to_chat_msgs(progression=["invalid_id"])


def test_remove_last_instruction_tool_schemas(message_manager):
    """Test removing tool schemas from last instruction"""
    instruction = message_manager.add_message(
        instruction="Test",
        tool_schemas={"tool1": {}, "tool2": {}},
        sender="user",
    )

    assert instruction.content.tool_schemas is not None
    assert len(instruction.content.tool_schemas) > 0
    message_manager.remove_last_instruction_tool_schemas()
    assert len(instruction.content.tool_schemas) == 0


def test_remove_last_instruction_tool_schemas_no_instruction(message_manager):
    """Test removing tool schemas when no instruction exists leaves state unchanged"""
    initial_count = len(message_manager.messages)
    message_manager.remove_last_instruction_tool_schemas()
    assert len(message_manager.messages) == initial_count


def test_concat_recent_action_responses_to_instruction(message_manager):
    """Test concatenating action responses to instruction"""
    instruction = message_manager.add_message(instruction="Test", context=[], sender="user")

    # Add action request and response
    request = message_manager.add_message(action_function="func", action_arguments={})
    response = message_manager.add_message(
        action_request=request, action_output={"result": "success"}
    )

    # Concat responses to instruction
    message_manager.concat_recent_action_responses_to_instruction(instruction)

    # Check that response content was added to instruction context
    assert len(instruction.content.prompt_context) > 0


def test_progression_property(message_manager):
    """Test progression property"""
    msg1 = message_manager.add_message(instruction="First", sender="user", recipient="assistant")
    msg2 = message_manager.add_message(
        assistant_response="Second", sender="assistant", recipient="user"
    )

    progress = message_manager.progression
    assert list(progress) == [msg1.id, msg2.id]


def test_message_manager_bool(message_manager):
    """Test bool evaluation of message manager"""
    assert not message_manager

    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")
    assert message_manager


def test_message_manager_contains(message_manager):
    """Test contains operator for message manager"""
    msg = message_manager.add_message(instruction="Test", sender="user", recipient="assistant")

    assert msg in message_manager

    other_msg = Instruction(content={"instruction": "Other"})
    assert other_msg not in message_manager


def test_message_manager_with_response_format(message_manager):
    """Test message manager with response format"""
    instruction = message_manager.add_message(
        instruction="Test",
        response_format=RequestModel,
        sender="user",
        recipient="assistant",
    )

    assert instruction.content.response_format == RequestModel
    assert instruction.content._structure_instance is not None
    assert instruction.content._structure_instance.base == RequestModel
    schema = instruction.content._structure_instance.request_schema()
    assert isinstance(schema, type)
    assert "name" in schema.model_fields


def test_message_manager_with_images(message_manager):
    """Test message manager with images"""
    instruction = message_manager.add_message(
        instruction="Describe this image",
        images=["image1.png", "image2.jpg"],
        image_detail="high",
        sender="user",
    )

    assert instruction.content.images == ["image1.png", "image2.jpg"]
    assert instruction.content.image_detail == "high"


def test_message_manager_with_tool_schemas(message_manager):
    """Test message manager with tool schemas"""
    tool_schemas = {
        "tool1": {"type": "function", "function": {"name": "tool1"}},
        "tool2": {"type": "function", "function": {"name": "tool2"}},
    }

    instruction = message_manager.add_message(
        instruction="Use these tools",
        tool_schemas=tool_schemas,
        sender="user",
    )

    # tool_schemas dict gets wrapped in a list
    assert instruction.content.tool_schemas == [tool_schemas]


def test_complete_conversation_flow(message_manager):
    """Test a complete conversation flow"""
    # Set system message
    system = message_manager.add_message(system="You are a helpful assistant")
    assert message_manager.system == system

    # User instruction
    instruction1 = message_manager.add_message(
        instruction="What is 2+2?", sender="user", recipient="assistant"
    )

    # Assistant response
    response1 = message_manager.add_message(
        assistant_response="2+2 equals 4", sender="assistant", recipient="user"
    )

    # User follow-up
    instruction2 = message_manager.add_message(
        instruction="What about 3+3?", sender="user", recipient="assistant"
    )

    # Assistant with action request
    request = message_manager.add_message(
        action_function="calculate",
        action_arguments={"a": 3, "b": 3},
        sender="assistant",
    )

    # Action response
    action_response = message_manager.add_message(
        action_request=request,
        action_output={"result": 6},
    )

    # Final assistant response
    response2 = message_manager.add_message(
        assistant_response="3+3 equals 6", sender="assistant", recipient="user"
    )

    # Verify the flow
    assert len(message_manager.messages) == 7
    assert message_manager.last_instruction == instruction2
    assert message_manager.last_response == response2
    assert len(message_manager.instructions) == 2
    assert len(message_manager.assistant_responses) == 2
    assert len(message_manager.action_requests) == 1
    assert len(message_manager.action_responses) == 1
    assert len(message_manager.actions) == 2

    # Test chat conversion
    chat_msgs = message_manager.to_chat_msgs()
    assert len(chat_msgs) == 7
