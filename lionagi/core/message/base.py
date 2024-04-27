from enum import Enum
from pydantic import Field, field_validator
from lionagi.libs import convert

from ..generic import Mail, BaseNode, BaseComponent

# List of system fields to exclude from additional context processing
SYSTEM_FIELDS = [
    "id_", "node_id", "meta", "metadata", "timestamp", "content", 
    "page_content", "assignment", "assignments", "instruction", 
    "context", "response_format", "sender", "recipient", "output_fields"
]


# Enums for defining message fields and roles
class MessageField(Enum):
    """
    Enum to store message fields for consistent referencing.
    """
    NODE_ID = "node_id"
    TIMESTAMP = "timestamp"
    ROLE = "role"
    SENDER = "sender"
    RECIPIENT = "recipient"
    CONTENT = "content"

class MessageRole(Enum):
    """
    Enum for possible roles a message can assume in a conversation.
    """
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


# Base class for messages
class BaseMessage(BaseNode, Mail):
    """
    A base class representing a message with validators and properties.
    """
    
    role: MessageRole | None = Field(
        default=None,
        description="The role of the message in the conversation.",
        examples=["system", "user", "assistant"]
    )
    
    @property
    def chat_msg(self):
        try:
            return self._check_chat_msg()
        except:
            return None

    def _check_chat_msg(self):
        if not self.role:
            raise ValueError("Message role not set")
        try:
            role = self.role.value if isinstance(self.role, MessageRole) else self.role
            content_str = convert.to_str(self.content)
            return {role: content_str}
        except Exception as e:
            raise ValueError(f"Failed to convert {self.content} into a string value") from e
        
    def __str__(self):
        """
        Provides a string representation of the message with content preview.
        """
        content_preview = (f"{self.content[:75]}..." if len(str(self.content)) > 75 
                           else str(self.content))
        return f"Message(role={self.role}, sender={self.sender}, content='{content_preview}')"