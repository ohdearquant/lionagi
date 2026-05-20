import pytest
from pydantic import BaseModel

from lionagi.protocols.types import (
    ActionRequest,
    ActionResponse,
    AssistantResponse,
    Instruction,
    MessageManager,
    MessageRole,
    Pile,
    System,
)


class RequestModel(BaseModel):
    """Model for testing request fields"""

    name: str
    age: int


@pytest.fixture
def message_manager():
    """Fixture providing a clean MessageManager instance"""
    return MessageManager()


"""Tests for MessageManager initialization and message creation."""


def test_message_manager_initialization():
    """Test basic initialization of MessageManager"""
    manager = MessageManager()
    assert isinstance(manager.messages, Pile)
    assert not manager.messages
    assert manager.system is None


def test_message_manager_initialization_with_messages():
    """Test MessageManager initialization with existing messages"""
    instruction = Instruction(content={"instruction": "Test"})
    manager = MessageManager(messages=[instruction])

    assert len(manager.messages) == 1
    assert instruction in manager.messages


def test_message_manager_initialization_with_dict_messages():
    """Test MessageManager initialization with dict messages"""
    instruction_dict = Instruction(content={"instruction": "Test"}).to_dict()
    manager = MessageManager(messages=[instruction_dict])

    assert len(manager.messages) == 1


def test_message_manager_with_system():
    """Test MessageManager initialization with system message"""
    system = System(content={"system_message": "Test system"})
    manager = MessageManager(system=system)

    assert manager.system == system
    assert system in manager.messages
    assert len(manager.messages) == 1


def test_message_manager_with_invalid_system():
    """Test MessageManager with invalid system type"""
    with pytest.raises(ValueError, match="System message must be a System instance"):
        MessageManager(system="not a system object")


def test_set_system():
    """Test setting and replacing system message"""
    manager = MessageManager()
    system1 = System(content={"system_message": "System 1"})
    system2 = System(content={"system_message": "System 2"})

    # Set first system
    manager.set_system(system1)
    assert manager.system == system1
    assert system1 in manager.messages
    assert len(manager.messages) == 1

    # Replace with second system
    manager.set_system(system2)
    assert manager.system == system2
    assert system1 not in manager.messages
    assert system2 in manager.messages
    assert len(manager.messages) == 1


def test_create_instruction_basic():
    """Test creating basic instruction message"""
    instruction = Instruction(
        content={"instruction": "Test instruction"},
        sender="user",
        recipient="assistant",
    )

    assert isinstance(instruction, Instruction)
    assert instruction.content.instruction == "Test instruction"
    assert instruction.sender == "user"
    assert instruction.recipient == "assistant"


def test_create_instruction_with_all_params():
    """Test creating instruction with all parameters"""
    instruction = Instruction(
        content={
            "instruction": "Test instruction",
            "context": {"test": "context"},
            "guidance": "Test guidance",
            "images": ["image1.png"],
            "request_fields": {"field1": "value1"},
            "plain_content": "Plain text",
            "image_detail": "high",
            "response_format": RequestModel,
            "tool_schemas": {"tool1": {}},
        },
        sender="user",
        recipient="assistant",
    )

    assert isinstance(instruction, Instruction)
    assert instruction.content.instruction == "Test instruction"
    assert instruction.content.guidance == "Test guidance"
    assert instruction.sender == "user"
    assert instruction.recipient == "assistant"
    assert instruction.content.image_detail == "high"
    # response_format as BaseModel: stores class internally, returns original
    assert instruction.content._model_class == RequestModel
    assert instruction.content.request_model == RequestModel
    assert instruction.content.response_format == RequestModel


def test_create_instruction_update_existing():
    """Test updating existing instruction"""
    instruction = Instruction(content={"instruction": "Original"})
    instruction.update(guidance="New guidance", sender="user")

    assert instruction.content.guidance == "New guidance"
    assert instruction.sender == MessageRole.USER


def test_create_instruction_default_context_extend(message_manager):
    """Updating instruction without handle flag should extend context."""
    instruction = message_manager.create_instruction(
        instruction="First",
        context=["base"],
    )

    message_manager.create_instruction(
        instruction=instruction,
        context=["new"],
    )

    assert instruction.content.context == ["base", "new"]


def test_create_instruction_context_replace(message_manager):
    """handle_context='replace' should overwrite existing context."""
    instruction = message_manager.create_instruction(
        instruction="First",
        context=["base"],
    )

    message_manager.create_instruction(
        instruction=instruction,
        context=["new"],
        handle_context="replace",
    )

    assert instruction.content.context == ["new"]


def test_create_instruction_response_format_instance(message_manager):
    """BaseModel instances for response_format should be accepted."""

    class InstanceModel(BaseModel):
        value: int

    instruction = message_manager.create_instruction(
        instruction="Test",
        response_format=InstanceModel(value=3),
    )

    # response_format stores the instance, internal fields handle the rest
    assert isinstance(instruction.content.response_format, InstanceModel)
    assert instruction.content.response_format.value == 3
    assert instruction.content._model_class == InstanceModel
    assert instruction.content.request_model == InstanceModel


def test_add_message_instruction_context_extend(message_manager):
    """add_message defaults to extending context when updating."""
    initial = message_manager.add_message(
        instruction="Original",
        context=["base"],
    )

    message_manager.add_message(
        instruction=initial,
        context=["new"],
    )

    assert initial.content.context == ["base", "new"]


def test_add_message_instruction_context_replace(message_manager):
    """add_message can replace context when handle_context='replace'."""
    initial = message_manager.add_message(
        instruction="Original",
        context=["base"],
    )

    message_manager.add_message(
        instruction=initial,
        context=["new"],
        handle_context="replace",
    )

    assert initial.content.context == ["new"]


def test_create_system_basic():
    """Test creating basic system message"""
    system = System(
        content={"system_message": "Test system"},
        sender="system",
        recipient="user",
    )

    assert isinstance(system, System)
    assert system.content.system_message == "Test system"
    assert system.sender == "system"
    assert system.recipient == "user"


def test_create_system_with_datetime():
    """Test creating system message with datetime"""
    system = System(content={"system_message": "Test system", "system_datetime": True})

    assert isinstance(system, System)
    # System datetime should be included in the message


def test_create_system_update_existing():
    """Test updating existing system message"""
    system = System(content={"system_message": "Original"})
    system.update(sender="system")

    assert system.sender == MessageRole.SYSTEM


def test_create_assistant_response_basic():
    """Test creating basic assistant response"""
    response = AssistantResponse(
        content={"assistant_response": "Test response"},
        sender="assistant",
        recipient="user",
    )

    assert isinstance(response, AssistantResponse)
    assert response.content.assistant_response == "Test response"
    assert response.sender == "assistant"
    assert response.recipient == "user"


def test_create_assistant_response_update_existing():
    """Test updating existing assistant response"""
    response = AssistantResponse(content={"assistant_response": "Original"})
    response.update(sender="assistant")

    assert response.sender == MessageRole.ASSISTANT


def test_create_action_request_basic():
    """Test creating basic action request"""
    request = ActionRequest(
        content={"function": "test_function", "arguments": {"arg": "value"}},
        sender="user",
        recipient="system",
    )

    assert isinstance(request, ActionRequest)
    assert request.content.function == "test_function"
    assert request.content.arguments == {"arg": "value"}
    assert request.sender == "user"
    assert request.recipient == "system"


def test_create_action_request_update_existing():
    """Test updating existing action request"""
    request = ActionRequest(
        content={"function": "original", "arguments": {}},
        sender="user",
        recipient="system",
    )
    request.content.function = "updated_function"

    assert request.content.function == "updated_function"


def test_create_action_response_basic():
    """Test creating basic action response"""
    request = ActionRequest(
        content={"function": "test", "arguments": {}},
        sender="user",
        recipient="system",
    )
    response = ActionResponse(
        content={
            "function": "test",
            "arguments": {},
            "output": {"result": "success"},
            "action_request_id": request.id,
        },
        sender="system",
        recipient="user",
    )
    # Link the request to the response
    request.content.action_response_id = response.id

    assert isinstance(response, ActionResponse)
    assert response.content.output == {"result": "success"}
    assert request.is_responded()


def test_create_action_response_without_request():
    """Test that action response requires valid request ID"""
    # ActionResponse can be created without a request, but won't have proper linking
    response = ActionResponse(
        content={
            "function": "test",
            "arguments": {},
            "output": {"result": "success"},
        }
    )
    # Without action_request_id, the response is valid but unlinked
    assert isinstance(response, ActionResponse)
    assert response.content.action_request_id is None


def test_create_action_response_update_existing():
    """Test updating existing action response"""
    request = ActionRequest(
        content={"function": "test", "arguments": {}},
        sender="user",
        recipient="system",
    )
    response = ActionResponse(
        content={
            "function": "test",
            "arguments": {},
            "output": {"old": "data"},
            "action_request_id": request.id,
        }
    )
    response.content.output = {"new": "data"}

    assert response.content.output == {"new": "data"}


def test_add_message_instruction(message_manager):
    """Test adding instruction via add_message"""
    instruction = message_manager.add_message(
        instruction="Test instruction",
        context={"key": "value"},
        guidance="Some guidance",
        sender="user",
        recipient="assistant",
    )

    assert isinstance(instruction, Instruction)
    assert instruction in message_manager.messages
    assert len(message_manager.messages) == 1
    assert instruction.content.instruction == "Test instruction"


def test_add_message_system(message_manager):
    """Test adding system message via add_message"""
    system = message_manager.add_message(
        system="Test system",
        sender="system",
        recipient="user",
    )

    assert isinstance(system, System)
    assert system in message_manager.messages
    assert message_manager.system == system
    assert len(message_manager.messages) == 1


def test_add_message_assistant_response(message_manager):
    """Test adding assistant response via add_message"""
    response = message_manager.add_message(
        assistant_response="Test response",
        sender="assistant",
        recipient="user",
    )

    assert isinstance(response, AssistantResponse)
    assert response in message_manager.messages
    assert len(message_manager.messages) == 1


def test_add_message_action_request(message_manager):
    """Test adding action request via add_message"""
    request = message_manager.add_message(
        action_function="test_function",
        action_arguments={"arg": "value"},
        sender="user",
        recipient="system",
    )

    assert isinstance(request, ActionRequest)
    assert request in message_manager.messages
    assert len(message_manager.messages) == 1


def test_add_message_action_response(message_manager):
    """Test adding action response via add_message"""
    # First create and add a request
    request = message_manager.add_message(
        action_function="test_function",
        action_arguments={},
        sender="user",
        recipient="system",
    )

    # Now add the response
    response = message_manager.add_message(
        action_request=request,
        action_output={"result": "success"},
        sender="system",
        recipient="user",
    )

    assert isinstance(response, ActionResponse)
    assert response in message_manager.messages
    assert len(message_manager.messages) == 2
    assert request.is_responded()


def test_add_message_with_metadata(message_manager):
    """Test adding message with metadata"""
    metadata = {"custom_key": "custom_value", "priority": "high"}
    msg = message_manager.add_message(
        instruction="Test",
        metadata=metadata,
        sender="user",
        recipient="assistant",
    )

    assert msg.metadata["extra"] == metadata


def test_add_message_update_existing(message_manager):
    """Test updating existing message via add_message"""
    # Add initial message
    msg1 = message_manager.add_message(
        instruction="First version",
        sender="user",
    )

    # Update the same message
    msg2 = message_manager.add_message(
        instruction=msg1,
        guidance="Added guidance",
    )

    assert msg1 is msg2  # Same object
    assert len(message_manager.messages) == 1
    assert msg2.content.guidance == "Added guidance"


def test_add_message_multiple_types_error(message_manager):
    """Test that adding multiple message types raises error"""
    with pytest.raises(
        ValueError, match="Only one message type can be added at a time"
    ):
        message_manager.add_message(
            instruction="Test",
            assistant_response="Response",
            sender="user",
            recipient="assistant",
        )


def test_add_message_system_instruction_error(message_manager):
    """Test that adding system and instruction together raises error"""
    with pytest.raises(
        ValueError, match="Only one message type can be added at a time"
    ):
        message_manager.add_message(
            system="System message",
            instruction="Instruction",
        )


def test_sync_add_message_rejects_async_hook(message_manager):
    """Sync add_message must refuse to silently drop async on_message_added
    callbacks — otherwise live SQLite persistence would lose writes while
    the runtime emits only a 'coroutine was never awaited' RuntimeWarning."""

    async def async_hook(_msg):  # pragma: no cover — never invoked
        pass

    message_manager._on_message_added.append(async_hook)

    with pytest.raises(RuntimeError, match="Async on_message_added callback"):
        message_manager.add_message(instruction="hi", sender="user", recipient="x")


def test_sync_add_message_accepts_sync_hook(message_manager):
    """Sync hooks fire from the sync add_message path as expected."""
    fired: list = []

    def sync_hook(msg):
        fired.append(msg)

    message_manager._on_message_added.append(sync_hook)

    msg = message_manager.add_message(
        instruction="hi", sender="user", recipient="x"
    )
    assert fired == [msg]


def test_sync_add_message_preflight_does_not_mutate_pile(message_manager):
    """R4-A MED-1: the async-hook guard must fire BEFORE pile mutation.

    Pre-fix: add_message created and inserted the message, THEN
    _fire_on_message_added raised. Caller catching the error would
    continue with an in-memory message that was never persisted (the
    live SQLite hook never ran).
    """
    async def async_hook(_msg):  # pragma: no cover — never invoked
        pass

    message_manager._on_message_added.append(async_hook)
    msgs_before = len(message_manager.messages)

    with pytest.raises(RuntimeError, match="Async on_message_added callback"):
        message_manager.add_message(instruction="hi", sender="u", recipient="x")

    # Critical: the pile was NOT mutated. No in-memory drift vs SQLite.
    assert len(message_manager.messages) == msgs_before


async def test_a_add_message_action_response_with_empty_output(message_manager):
    """R4-A HIGH-3: an ActionResponse with falsey output (empty string,
    0, False, [], {}) used to be silently dropped because the dispatch
    branch tested ``action_output`` truthiness instead of
    ``action_output is not None``. The ActionRequest would then be
    re-emitted as a duplicate via the fallback branch.
    """
    from lionagi.protocols.messages import ActionRequest, ActionResponse

    # Build a real ActionRequest so the response can reference it.
    req = ActionRequest(
        content={"function": "ls", "arguments": {}},
        sender="x",
        recipient="user",
    )
    await message_manager.a_add_message(action_request=req)
    requests_before = sum(
        1 for m in message_manager.messages if isinstance(m, ActionRequest)
    )

    # Feed the response with empty-string output — the common case for
    # a successful shell command with no stdout.
    await message_manager.a_add_message(
        action_request=req,
        action_output="",  # falsey but valid
        sender="user",
        recipient="x",
    )

    requests_after = sum(
        1 for m in message_manager.messages if isinstance(m, ActionRequest)
    )
    responses_after = sum(
        1 for m in message_manager.messages if isinstance(m, ActionResponse)
    )

    # The ActionRequest was NOT duplicated, and an ActionResponse WAS
    # created for the empty output.
    assert requests_after == requests_before, (
        "ActionRequest was re-emitted (truthy fallback fired)"
    )
    assert responses_after == 1, (
        "ActionResponse for empty output was dropped"
    )


async def test_a_add_message_passes_through_prebuilt_action_response(
    message_manager,
):
    """When run.py passes a fully-built ActionResponse (with is_error
    already set), the manager must use that object — not create a new
    one — so the persistence hook observes the final state.
    """
    from lionagi.protocols.messages import ActionRequest

    req = ActionRequest(
        content={"function": "rm", "arguments": {"path": "/etc/passwd"}},
        sender="x",
        recipient="user",
    )
    await message_manager.a_add_message(action_request=req)

    prebuilt = MessageManager.create_action_response(
        action_request=req,
        action_output={"error": "permission denied"},
        sender="user",
        recipient="x",
    )
    prebuilt.metadata["is_error"] = True

    captured = []

    async def hook(msg):
        captured.append((type(msg).__name__, dict(msg.metadata)))

    message_manager._on_message_added.append(hook)
    await message_manager.a_add_message(
        action_request=req,
        action_output={"error": "permission denied"},
        action_response=prebuilt,
        sender="user",
        recipient="x",
    )

    # The hook observed an ActionResponse whose metadata['is_error']
    # was already True at hook time (the bug R4 closed: live persist
    # used to see is_error=False and serialize the wrong state).
    assert any(
        kind == "ActionResponse" and meta.get("is_error") is True
        for kind, meta in captured
    ), f"hook never observed final is_error state: {captured}"
