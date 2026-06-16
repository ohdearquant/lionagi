import pytest
from pydantic import BaseModel

from lionagi.protocols.types import (
    Instruction,
    MessageManager,
    Pile,
)


class RequestModel(BaseModel):
    """Model for testing request fields"""

    name: str
    age: int


@pytest.fixture
def message_manager():
    """Fixture providing a clean MessageManager instance"""
    return MessageManager()


def test_clear_messages_no_system(message_manager):
    """Test clearing messages when no system message exists"""
    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")
    message_manager.add_message(assistant_response="Response", sender="assistant", recipient="user")

    assert len(message_manager.messages) == 2
    message_manager.clear_messages()
    assert len(message_manager.messages) == 0


def test_clear_messages_preserves_system(message_manager):
    """Test clearing messages preserves system message"""
    system = message_manager.add_message(system="Test system")
    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")
    message_manager.add_message(assistant_response="Response", sender="assistant", recipient="user")

    assert len(message_manager.messages) == 3
    message_manager.clear_messages()
    assert len(message_manager.messages) == 1
    assert system in message_manager.messages


async def test_async_add_message(message_manager):
    """Test async message addition"""
    msg = await message_manager.a_add_message(
        instruction="Test", sender="user", recipient="assistant"
    )
    assert isinstance(msg, Instruction)
    assert len(message_manager.messages) == 1


async def test_async_clear_messages(message_manager):
    """Test async clear messages"""
    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")
    assert len(message_manager.messages) == 1

    await message_manager.aclear_messages()
    assert len(message_manager.messages) == 0


def test_last_response(message_manager):
    """Test last_response property"""
    assert message_manager.last_response is None

    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")
    assert message_manager.last_response is None

    response1 = message_manager.add_message(
        assistant_response="Response 1", sender="assistant", recipient="user"
    )
    assert message_manager.last_response == response1

    response2 = message_manager.add_message(
        assistant_response="Response 2", sender="assistant", recipient="user"
    )
    assert message_manager.last_response == response2


def test_last_instruction(message_manager):
    """Test last_instruction property"""
    assert message_manager.last_instruction is None

    instruction1 = message_manager.add_message(
        instruction="First", sender="user", recipient="assistant"
    )
    assert message_manager.last_instruction == instruction1

    message_manager.add_message(assistant_response="Response", sender="assistant", recipient="user")
    assert message_manager.last_instruction == instruction1

    instruction2 = message_manager.add_message(
        instruction="Second", sender="user", recipient="assistant"
    )
    assert message_manager.last_instruction == instruction2


def test_assistant_responses_property(message_manager):
    """Test assistant_responses property"""
    assert len(message_manager.assistant_responses) == 0

    message_manager.add_message(instruction="Test", sender="user", recipient="assistant")
    assert len(message_manager.assistant_responses) == 0

    response1 = message_manager.add_message(
        assistant_response="Response 1", sender="assistant", recipient="user"
    )
    response2 = message_manager.add_message(
        assistant_response="Response 2", sender="assistant", recipient="user"
    )

    responses = message_manager.assistant_responses
    assert isinstance(responses, Pile)
    assert len(responses) == 2
    assert response1 in responses
    assert response2 in responses


def test_instructions_property(message_manager):
    """Test instructions property"""
    assert len(message_manager.instructions) == 0

    instruction1 = message_manager.add_message(
        instruction="First", sender="user", recipient="assistant"
    )
    instruction2 = message_manager.add_message(
        instruction="Second", sender="user", recipient="assistant"
    )

    message_manager.add_message(assistant_response="Response", sender="assistant", recipient="user")

    instructions = message_manager.instructions
    assert isinstance(instructions, Pile)
    assert len(instructions) == 2
    assert instruction1 in instructions
    assert instruction2 in instructions


def test_action_requests_property(message_manager):
    """Test action_requests property"""
    assert len(message_manager.action_requests) == 0

    request1 = message_manager.add_message(
        action_function="func1", action_arguments={}, sender="user"
    )
    request2 = message_manager.add_message(
        action_function="func2", action_arguments={}, sender="user"
    )

    requests = message_manager.action_requests
    assert isinstance(requests, Pile)
    assert len(requests) == 2
    assert request1 in requests
    assert request2 in requests


def test_action_responses_property(message_manager):
    """Test action_responses property"""
    assert len(message_manager.action_responses) == 0

    request = message_manager.add_message(
        action_function="func", action_arguments={}, sender="user"
    )
    response = message_manager.add_message(
        action_request=request, action_output={"result": "success"}
    )

    responses = message_manager.action_responses
    assert isinstance(responses, Pile)
    assert len(responses) == 1
    assert response in responses


def test_actions_property(message_manager):
    """Test actions property (both requests and responses)"""
    assert len(message_manager.actions) == 0

    request = message_manager.add_message(
        action_function="func", action_arguments={}, sender="user"
    )
    assert len(message_manager.actions) == 1

    response = message_manager.add_message(
        action_request=request, action_output={"result": "success"}
    )

    actions = message_manager.actions
    assert isinstance(actions, Pile)
    assert len(actions) == 2
    assert request in actions
    assert response in actions
