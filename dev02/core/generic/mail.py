"""
This module defines classes for representing mail packages and mailboxes
in a messaging system.

The module includes the following classes:
- MailPackageCategory: An enumeration of categories for mail packages.
- Mail: Represents a mail message sent from one component to another.
- MailBox: Represents a mailbox that stores pending incoming and outgoing mails.
"""

from typing import Any
from enum import Enum

from pydantic import Field, field_validator

from lionagi.core.generic.abstract.component import BaseComponent


class MailPackageCategory(str, Enum):
    """
    Defines categories for mail packages in a messaging system.

    Attributes:
        MESSAGES: Represents general messages.
        TOOL: Represents tools.
        SERVICE: Represents services.
        MODEL: Represents models.
        NODE: Represents nodes.
        NODE_LIST: Represents a list of nodes.
        NODE_ID: Represents a node's ID.
        START: Represents a start signal or value.
        END: Represents an end signal or value.
        CONDITION: Represents a condition.
    """

    MESSAGES = "messages"
    TOOL = "tool"
    SERVICE = "service"
    MODEL = "model"
    NODE = "node"
    NODE_LIST = "node_list"
    NODE_ID = "node_id"
    START = "start"
    END = "end"
    CONDITION = "condition"


class Package(BaseComponent):
    category: MailPackageCategory = Field(
        ..., title="Category", description="The category of the mail package."
    )

    package: Any = Field(
        ..., title="Package", description="The package to send in the mail."
    )


class Mail(BaseComponent):
    """
    Represents a mail message sent from one component to another within
    the system.

    Attributes:
        sender (str): The ID of the sender node.
        recipient (str): The ID of the recipient node.
        category (MailPackageCategory): The category of the mail package.
        package (Any): The content of the mail package.
    """

    sender: str = Field(
        title="Sender",
        description="The id of the sender node.",
    )

    recipient: str = Field(
        title="Recipient",
        description="The id of the recipient node.",
    )

    packages: dict[str, Package] = Field(
        title="Packages",
        default_factory=dict,
        description="The packages to send in the mail.",
    )

    @field_validator("sender", "recipient", mode="before")
    def _validate_sender_recipient(cls, value):
        if isinstance(value, BaseComponent):
            return value.id_
        return value
