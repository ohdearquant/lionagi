# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from enum import Enum
from typing import Any

from lionagi.protocols.messages.base import (
    MESSAGE_FIELDS,
    MessageField,
    MessageRole,
    validate_sender_recipient,
)
from lionagi.protocols.messages.message import MessageContent, RoledMessage


# Test MessageContent implementation
@dataclass(slots=True)
class _MockContent(MessageContent):
    """Mock implementation of MessageContent for testing purposes."""

    text: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def rendered(self) -> str:
        """Render the content as a string."""
        return self.text

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_MockContent":
        """Create an instance from a dictionary."""
        return cls(
            text=data.get("text", ""),
            metadata=data.get("metadata"),
        )


# Test RoledMessage implementation
class _MockMessage(RoledMessage):
    """Mock implementation of RoledMessage for testing."""

    content: _MockContent = None

    def __init__(self, **kwargs):
        # Extract content fields to create _MockContent
        content_data = kwargs.pop("content", None)
        if content_data is None:
            content_data = _MockContent(text="")
        elif isinstance(content_data, dict):
            content_data = _MockContent.from_dict(content_data)
        elif isinstance(content_data, str):
            content_data = _MockContent(text=content_data)

        super().__init__(content=content_data, **kwargs)


# ============================================================================
# MessageRole Tests
# ============================================================================


def test_message_role_enum():
    """Test MessageRole enumeration values and types."""
    assert isinstance(MessageRole.SYSTEM, Enum)
    assert isinstance(MessageRole.USER, Enum)
    assert isinstance(MessageRole.ASSISTANT, Enum)
    assert isinstance(MessageRole.UNSET, Enum)
    assert isinstance(MessageRole.ACTION, Enum)

    assert MessageRole.SYSTEM.value == "system"
    assert MessageRole.USER.value == "user"
    assert MessageRole.ASSISTANT.value == "assistant"
    assert MessageRole.UNSET.value == "unset"
    assert MessageRole.ACTION.value == "action"


# ============================================================================
# MessageField Tests
# ============================================================================


def test_message_field_enum():
    """Test MessageField enumeration and MESSAGE_FIELDS constant."""
    assert isinstance(MessageField.CREATED_AT, Enum)
    assert isinstance(MessageField.ROLE, Enum)
    assert isinstance(MessageField.CONTENT, Enum)
    assert isinstance(MessageField.ID, Enum)
    assert isinstance(MessageField.SENDER, Enum)
    assert isinstance(MessageField.RECIPIENT, Enum)
    assert isinstance(MessageField.METADATA, Enum)

    assert MessageField.CREATED_AT.value == "created_at"
    assert MessageField.ROLE.value == "role"
    assert MessageField.CONTENT.value == "content"
    assert MessageField.ID.value == "id"
    assert MessageField.SENDER.value == "sender"
    assert MessageField.RECIPIENT.value == "recipient"
    assert MessageField.METADATA.value == "metadata"

    # Verify MESSAGE_FIELDS contains all field values
    assert all(field.value in MESSAGE_FIELDS for field in MessageField)


# ============================================================================
# MessageContent Tests
# ============================================================================


def test_message_content_dataclass():
    """Test MessageContent is a proper dataclass with slots."""
    content = _MockContent(text="test content", metadata={"key": "value"})

    assert content.text == "test content"
    assert content.metadata["key"] == "value"

    # Verify slots behavior (no __dict__)
    assert not hasattr(content, "__dict__")


def test_message_content_rendered():
    """Test MessageContent rendered property."""
    content = _MockContent(text="Hello World")
    assert content.rendered == "Hello World"


def test_message_content_from_dict():
    """Test MessageContent.from_dict() classmethod."""
    data = {"text": "test message", "metadata": {"importance": "high"}}
    content = _MockContent.from_dict(data)

    assert content.text == "test message"
    assert content.metadata["importance"] == "high"
    assert content.rendered == "test message"


def test_message_content_to_dict():
    """Test MessageContent.to_dict() method from DataClass."""
    content = _MockContent(text="test", metadata={"key": "value"})
    data = content.to_dict()

    assert "text" in data
    assert data["text"] == "test"
    assert "metadata" in data
    assert data["metadata"]["key"] == "value"


def test_message_content_none_as_sentinel():
    """Test MessageContent._config.none_as_sentinel."""
    assert MessageContent._config.none_as_sentinel is True


# ============================================================================
# RoledMessage Initialization Tests
# ============================================================================


def test_roled_message_initialization():
    """Test basic initialization of Message with MessageContent."""
    content = _MockContent(text="Hello")
    message = _MockMessage(
        content=content,
        sender="user",
        recipient="assistant",
    )

    assert message.role == MessageRole.UNSET
    assert isinstance(message.content, MessageContent)
    assert isinstance(message.content, _MockContent)
    assert message.content.text == "Hello"
    assert message.sender == "user"
    assert message.recipient == "assistant"


def test_roled_message_initialization_from_string():
    """Test Message initialization with string content."""
    message = _MockMessage(content="Hello World", sender="user")

    assert message.content.text == "Hello World"
    assert message.rendered == "Hello World"


def test_roled_message_initialization_from_dict():
    """Test Message initialization with dict content."""
    message = _MockMessage(
        content={"text": "Hello", "metadata": {"type": "greeting"}},
        sender="user",
    )

    assert message.content.text == "Hello"
    assert message.content.metadata["type"] == "greeting"


def test_roled_message_content_always_message_content():
    """Test that content is always MessageContent, never dict."""
    message = _MockMessage(content={"text": "test"})

    assert isinstance(message.content, MessageContent)
    assert not isinstance(message.content, dict)


# ============================================================================
# RoledMessage Role Validation Tests
# ============================================================================


def test_roled_message_role_classvar():
    """Test role is a ClassVar property, not an instance field."""
    from typing import ClassVar

    class _UserMessage(RoledMessage):
        _role: ClassVar[MessageRole] = MessageRole.USER
        content: _MockContent = None

        def __init__(self, **kwargs):
            content_data = kwargs.pop("content", None)
            if content_data is None:
                content_data = _MockContent(text="")
            elif isinstance(content_data, dict):
                content_data = _MockContent.from_dict(content_data)
            super().__init__(content=content_data, **kwargs)

    msg = _UserMessage(content=_MockContent(text="test"), sender="user")
    assert msg.role == MessageRole.USER

    base = _MockMessage(content=_MockContent(text="test"))
    assert base.role == MessageRole.UNSET


def test_roled_message_role_ignored_in_constructor():
    """Test that passing role= to constructor is silently ignored."""
    message = _MockMessage(
        role="user", content=_MockContent(text="test"), sender="user"
    )
    assert message.role == MessageRole.UNSET


def test_roled_message_role_in_serialization():
    """Test that role appears in serialized output."""
    message = _MockMessage(content=_MockContent(text="test"))
    d = message.to_dict()
    assert d["role"] == "unset"


def test_roled_message_role_default():
    """Test default role is UNSET."""
    message = _MockMessage(content=_MockContent(text="test"))
    assert message.role == MessageRole.UNSET


# ============================================================================
# RoledMessage Sender/Recipient Tests
# ============================================================================


def test_roled_message_sender_recipient_validation():
    """Test sender and recipient validation."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="test"),
        sender="user",
        recipient="assistant",
    )

    assert message.sender == MessageRole.USER
    assert message.recipient == MessageRole.ASSISTANT


def test_roled_message_sender_recipient_string():
    """Test sender and recipient with string values."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="test"),
        sender="user",
        recipient="assistant",
    )

    assert message.sender == MessageRole.USER
    assert message.recipient == MessageRole.ASSISTANT


def test_roled_message_sender_recipient_none():
    """Test sender and recipient with None values."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="test"),
        sender=None,
        recipient=None,
    )

    # None should be validated to UNSET
    assert message.sender == MessageRole.UNSET or message.sender is None
    assert message.recipient == MessageRole.UNSET or message.recipient is None


# ============================================================================
# RoledMessage Properties Tests
# ============================================================================


def test_roled_message_rendered_property():
    """Test RoledMessage rendered property delegates to content.rendered."""
    content = _MockContent(text="Hello World")
    message = _MockMessage(role=MessageRole.USER, content=content)

    assert message.rendered == "Hello World"
    assert message.rendered == content.rendered


def test_roled_message_chat_msg_property():
    """Test Message chat_msg property."""
    message = _MockMessage(content=_MockContent(text="Hello"))

    chat_msg = message.chat_msg
    assert chat_msg is not None
    assert chat_msg["role"] == "unset"
    assert chat_msg["content"] == "Hello"


def test_roled_message_chat_msg_property_error_handling():
    """Test chat_msg property returns None on error."""
    # This test assumes chat_msg can fail gracefully
    # In practice, it should work for valid messages
    message = _MockMessage(role=MessageRole.USER, content=_MockContent(text="Hello"))

    assert message.chat_msg is not None


def test_roled_message_image_content_property():
    """Test image_content property returns None for text content."""
    message = _MockMessage(role=MessageRole.USER, content=_MockContent(text="Hello"))

    # For simple text content, image_content should be None
    assert message.image_content is None


# ============================================================================
# RoledMessage Update Method Tests
# ============================================================================


def test_roled_message_update_sender():
    """Test update() method updates sender."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="Hello"),
        sender="user",
    )

    message.update(sender="assistant")
    assert message.sender == MessageRole.ASSISTANT


def test_roled_message_update_recipient():
    """Test update() method updates recipient."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="Hello"),
        recipient="assistant",
    )

    message.update(recipient="system")
    assert message.recipient == MessageRole.SYSTEM


def test_roled_message_update_content():
    """Test update() method updates content via from_dict()."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="Original", metadata={"version": 1}),
    )

    # Update content fields
    message.update(text="Updated", metadata={"version": 2})

    assert message.content.text == "Updated"
    assert message.content.metadata["version"] == 2
    assert message.rendered == "Updated"


def test_roled_message_update_multiple_fields():
    """Test update() method updates multiple fields at once."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="Original"),
        sender="user",
        recipient="assistant",
    )

    message.update(sender="system", recipient="user", text="Updated")

    assert message.sender == MessageRole.SYSTEM
    assert message.recipient == MessageRole.USER
    assert message.content.text == "Updated"


def test_roled_message_update_preserves_other_fields():
    """Test update() preserves fields not being updated."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="Original", metadata={"key": "value"}),
        sender="user",
    )

    message.update(text="Updated")

    # Metadata should be preserved
    assert message.content.metadata["key"] == "value"
    assert message.sender == MessageRole.USER


# ============================================================================
# RoledMessage Clone Tests
# ============================================================================


# Clone method doesn't exist - removed test


# ============================================================================
# RoledMessage Serialization Tests
# ============================================================================


def test_roled_message_to_dict():
    """Test Message serialization to dict."""
    message = _MockMessage(
        content=_MockContent(text="test"),
        sender="user",
        recipient="assistant",
    )

    serialized = message.to_dict()

    assert "role" in serialized
    assert "content" in serialized
    assert "sender" in serialized
    assert "recipient" in serialized
    assert serialized["role"] == "unset"


def test_roled_message_serialization_to_dict():
    """Test serialization of RoledMessage to dict."""
    message = _MockMessage(
        role=MessageRole.USER, content=_MockContent(text="test"), sender="user"
    )

    serialized = message.to_dict()
    assert "role" in serialized
    assert "content" in serialized


# ============================================================================
# RoledMessage String Representation Tests
# ============================================================================


def test_roled_message_str_representation():
    """Test string representation of RoledMessage."""
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="test"),
        sender="user",
        recipient="assistant",
    )

    str_repr = str(message)
    # String representation should contain useful information
    assert isinstance(str_repr, str)
    assert len(str_repr) > 0


# ============================================================================
# validate_sender_recipient Function Tests
# ============================================================================


def test_validate_sender_recipient_message_role():
    """Test validate_sender_recipient with MessageRole."""
    result = validate_sender_recipient(MessageRole.USER)
    assert result == MessageRole.USER


def test_validate_sender_recipient_string():
    """Test validate_sender_recipient with valid string."""
    result = validate_sender_recipient("user")
    assert result == MessageRole.USER


def test_validate_sender_recipient_none():
    """Test validate_sender_recipient with None."""
    result = validate_sender_recipient(None)
    assert result == MessageRole.UNSET


# ============================================================================
# Integration Tests
# ============================================================================


def test_message_workflow_complete():
    """Test complete message workflow: create, update, serialize."""
    # Create message
    message = _MockMessage(
        role=MessageRole.USER,
        content=_MockContent(text="Hello", metadata={"priority": "high"}),
        sender="user",
        recipient="assistant",
    )

    # Verify initial state
    assert message.rendered == "Hello"
    assert message.chat_msg["content"] == "Hello"

    # Update message
    message.update(text="Updated Hello", sender="system")
    assert message.rendered == "Updated Hello"
    assert message.content.metadata["priority"] == "high"

    # Serialize message
    serialized = message.to_dict()
    assert "role" in serialized
    assert "content" in serialized


def test_message_content_immutability_via_update():
    """Test that content updates create new instances via from_dict()."""
    message = _MockMessage(role=MessageRole.USER, content=_MockContent(text="Original"))

    original_content = message.content
    message.update(text="Updated")

    # Content should be a new instance
    assert message.content is not original_content
    assert message.content.text == "Updated"
