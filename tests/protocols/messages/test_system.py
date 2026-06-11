from datetime import datetime

import pytest

from lionagi.protocols.messages.system import SystemContent
from lionagi.protocols.types import MessageRole, System

# ============================================================================
# SystemContent Tests
# ============================================================================


def test_systemcontent_initialization():
    """Test basic initialization of SystemContent dataclass"""
    # Default initialization
    content = SystemContent()
    assert content.system_message == "You are a helpful AI assistant. Let's think step by step."
    assert content.system_datetime is None

    # Custom message
    content = SystemContent(system_message="Custom message")
    assert content.system_message == "Custom message"
    assert content.system_datetime is None

    # With datetime
    content = SystemContent(system_message="Test message", system_datetime="2024-01-01T12:00")
    assert content.system_message == "Test message"
    assert content.system_datetime == "2024-01-01T12:00"


def test_systemcontent_slots():
    """Test that SystemContent uses slots for memory efficiency"""
    content = SystemContent()

    # Should not have __dict__ due to slots=True
    assert not hasattr(content, "__dict__")

    # Should not be able to add arbitrary attributes
    with pytest.raises(AttributeError):
        content.new_attribute = "value"


def test_systemcontent_rendered_without_datetime():
    """Test rendered property without datetime"""
    content = SystemContent(system_message="Test message")

    rendered = content.rendered
    assert rendered == "Test message"
    assert "System Time:" not in rendered


def test_systemcontent_rendered_with_datetime():
    """Test rendered property with datetime"""
    content = SystemContent(system_message="Test message", system_datetime="2024-01-01T12:00")

    rendered = content.rendered
    assert "System Time: 2024-01-01T12:00" in rendered
    assert "Test message" in rendered
    assert rendered.startswith("System Time:")


def test_systemcontent_from_dict_basic():
    """Test SystemContent.from_dict() with basic data"""
    data = {"system_message": "Custom message"}
    content = SystemContent.from_dict(data)

    assert content.system_message == "Custom message"
    assert content.system_datetime is None


def test_systemcontent_from_dict_with_datetime_true():
    """Test SystemContent.from_dict() with system_datetime=True"""
    data = {"system_message": "Test", "system_datetime": True}
    content = SystemContent.from_dict(data)

    assert content.system_message == "Test"
    assert content.system_datetime is not None

    # Should be ISO format with minutes precision
    # Parse to verify format
    dt = datetime.fromisoformat(content.system_datetime)
    assert isinstance(dt, datetime)


def test_systemcontent_from_dict_with_datetime_false():
    """Test SystemContent.from_dict() with system_datetime=False"""
    data = {"system_message": "Test", "system_datetime": False}
    content = SystemContent.from_dict(data)

    assert content.system_message == "Test"
    assert content.system_datetime is None


def test_systemcontent_from_dict_with_datetime_none():
    """Test SystemContent.from_dict() with system_datetime=None"""
    data = {"system_message": "Test", "system_datetime": None}
    content = SystemContent.from_dict(data)

    assert content.system_message == "Test"
    assert content.system_datetime is None


def test_systemcontent_from_dict_with_custom_datetime():
    """Test SystemContent.from_dict() with custom datetime string"""
    custom_datetime = "2024-01-01T12:00:00"
    data = {"system_message": "Test", "system_datetime": custom_datetime}
    content = SystemContent.from_dict(data)

    assert content.system_message == "Test"
    assert content.system_datetime == custom_datetime


def test_systemcontent_from_dict_empty():
    """Test SystemContent.from_dict() with empty dict uses defaults"""
    data = {}
    content = SystemContent.from_dict(data)

    assert content.system_message == "You are a helpful AI assistant. Let's think step by step."
    assert content.system_datetime is None


def test_systemcontent_from_dict_partial():
    """Test SystemContent.from_dict() with partial data"""
    data = {"system_datetime": "2024-01-01T12:00"}
    content = SystemContent.from_dict(data)

    # Should use default system_message
    assert content.system_message == "You are a helpful AI assistant. Let's think step by step."
    assert content.system_datetime == "2024-01-01T12:00"


# ============================================================================
# System Message Tests
# ============================================================================


def test_system_initialization():
    """Test basic initialization of System"""
    system_msg = "Test system message"
    system = System(content=SystemContent(system_message=system_msg))

    assert system.role == MessageRole.SYSTEM
    assert system_msg in system.rendered
    assert system.sender == MessageRole.SYSTEM
    assert system.recipient == MessageRole.ASSISTANT
    assert isinstance(system.content, SystemContent)


def test_system_with_datetime_true():
    """Test System with datetime generation (datetime=True)"""
    system_msg = "Test system message"
    system = System(content={"system_message": system_msg, "system_datetime": True})

    assert "System Time:" in system.rendered
    assert system_msg in system.rendered
    assert system.content.system_datetime is not None

    # Verify datetime is ISO format
    dt = datetime.fromisoformat(system.content.system_datetime)
    assert isinstance(dt, datetime)


def test_system_with_datetime_false():
    """Test System with datetime=False"""
    system = System(content={"system_message": "Test", "system_datetime": False})

    assert system.content.system_datetime is None
    assert "System Time:" not in system.rendered


def test_system_with_datetime_none():
    """Test System with datetime=None (default)"""
    system = System(content={"system_message": "Test"})

    assert system.content.system_datetime is None
    assert "System Time:" not in system.rendered


def test_system_with_custom_datetime():
    """Test System with custom datetime string"""
    custom_datetime = "2024-01-01T12:00:00"
    system = System(content={"system_message": "Test", "system_datetime": custom_datetime})

    assert system.content.system_datetime == custom_datetime
    assert custom_datetime in system.rendered
    assert "System Time:" in system.rendered


def test_system_update():
    """Test updating System message"""
    system = System(content={"system_message": "Initial message"})

    # Update system message
    new_message = "Updated message"
    system.update(system_message=new_message)
    assert new_message in system.rendered

    # Update sender and recipient
    system.update(sender="system", recipient="user")
    assert system.sender == MessageRole.SYSTEM
    assert system.recipient == MessageRole.USER

    # Update with datetime
    system.update(system_message="Test", system_datetime=True)
    assert "System Time:" in system.rendered
    assert system.content.system_datetime is not None


def test_system_content_format():
    """Test System content formatting"""
    system = System(content={"system_message": "Test message"})

    formatted = system.chat_msg
    assert formatted["role"] == MessageRole.SYSTEM.value
    assert formatted["content"] == system.rendered


def test_system_with_default_message():
    """Test System with default message"""
    system = System(content={})

    assert "You are a helpful AI assistant" in system.rendered
    assert "Let's think step by step" in system.rendered


# Clone method doesn't exist - removed test


def test_system_str_representation():
    """Test string representation of System"""
    system = System(content={"system_message": "Test message"})

    str_repr = str(system)
    assert "Message" in str_repr
    # Check for either role=system or role=MessageRole.SYSTEM depending on implementation
    assert "role=system" in str_repr.lower() or "system" in str_repr.lower()
    assert "Test message" in str_repr


def test_system_message_load():
    """Test loading System from dictionary"""
    protected_params = {
        "role": MessageRole.SYSTEM,
        "content": {"system_message": "Test message"},
        "sender": "system",
        "recipient": "user",
    }

    system = System.from_dict(protected_params)

    assert system.role == MessageRole.SYSTEM
    assert "Test message" in system.rendered
    assert system.sender == "system"
    assert system.recipient == "user"


def test_system_validation():
    """Test System validation"""
    # Test with invalid sender/recipient
    with pytest.raises(ValueError):
        System(content={"system_message": "Test"}, sender=123)

    with pytest.raises(ValueError):
        System(content={"system_message": "Test"}, recipient=123)


def test_system_serialization():
    """Test System serialization"""
    system = System(content={"system_message": "Test message", "system_datetime": True})

    serialized = system.model_dump()

    assert serialized["role"] == MessageRole.SYSTEM.value
    assert isinstance(serialized["content"], dict)
    assert "system_message" in serialized["content"]
    assert "system_datetime" in serialized["content"]


def test_system_chat_message():
    """Test System chat message format"""
    system = System(content={"system_message": "Test message"})

    chat_msg = system.chat_msg
    assert chat_msg["role"] == "system"
    assert chat_msg["content"] == system.rendered


def test_system_content_validator_with_none():
    """Test System content validator handles None"""
    system = System(content=None)

    assert isinstance(system.content, SystemContent)
    assert (
        system.content.system_message == "You are a helpful AI assistant. Let's think step by step."
    )


def test_system_content_validator_with_dict():
    """Test System content validator handles dict"""
    content_dict = {
        "system_message": "Custom message",
        "system_datetime": "2024-01-01T12:00",
    }
    system = System(content=content_dict)

    assert isinstance(system.content, SystemContent)
    assert system.content.system_message == "Custom message"
    assert system.content.system_datetime == "2024-01-01T12:00"


def test_system_content_validator_with_systemcontent():
    """Test System content validator handles SystemContent instance"""
    content = SystemContent(system_message="Custom message", system_datetime="2024-01-01T12:00")
    system = System(content=content)

    assert system.content is content
    assert system.content.system_message == "Custom message"


def test_system_content_validator_with_invalid_type():
    """Test System content validator rejects invalid types"""
    with pytest.raises(TypeError):
        System(content="invalid string")

    with pytest.raises(TypeError):
        System(content=123)


# ============================================================================
# Integration Tests
# ============================================================================


def test_system_complete_workflow():
    """Test complete workflow with System and SystemContent"""
    # Create system with datetime
    system = System(
        content={
            "system_message": "You are an expert Python developer.",
            "system_datetime": True,
        }
    )

    # Verify structure
    assert isinstance(system.content, SystemContent)
    assert system.content.system_message == "You are an expert Python developer."
    assert system.content.system_datetime is not None

    # Verify rendering
    rendered = system.rendered
    assert "System Time:" in rendered
    assert "You are an expert Python developer." in rendered

    # Verify chat format
    chat_msg = system.chat_msg
    assert chat_msg["role"] == "system"
    assert chat_msg["content"] == rendered

    # Verify serialization
    serialized = system.model_dump()
    assert serialized["content"]["system_message"] == "You are an expert Python developer."
    assert serialized["content"]["system_datetime"] is not None


def test_system_datetime_scenarios():
    """Test all datetime scenarios in one comprehensive test"""
    # Scenario 1: datetime=True (auto-generate)
    sys1 = System(content={"system_message": "Test", "system_datetime": True})
    assert sys1.content.system_datetime is not None
    assert "System Time:" in sys1.rendered

    # Scenario 2: datetime=False (explicit None)
    sys2 = System(content={"system_message": "Test", "system_datetime": False})
    assert sys2.content.system_datetime is None
    assert "System Time:" not in sys2.rendered

    # Scenario 3: datetime=None (default behavior)
    sys3 = System(content={"system_message": "Test", "system_datetime": None})
    assert sys3.content.system_datetime is None
    assert "System Time:" not in sys3.rendered

    # Scenario 4: Custom datetime string
    sys4 = System(
        content={
            "system_message": "Test",
            "system_datetime": "2024-12-25T09:00",
        }
    )
    assert sys4.content.system_datetime == "2024-12-25T09:00"
    assert "System Time: 2024-12-25T09:00" in sys4.rendered

    # Scenario 5: No datetime parameter (default)
    sys5 = System(content={"system_message": "Test"})
    assert sys5.content.system_datetime is None
    assert "System Time:" not in sys5.rendered
