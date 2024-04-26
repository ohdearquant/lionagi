from collections import deque
from pydantic import Field
from pydantic.dataclasses import dataclass

from lionagi.core.generic.mail import Mail

from collections import deque
from enum import Enum

from lionagi.core.generic import Node


class MailCategory(str, Enum):
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


class BaseMail:

    def __init__(self, sender_id, recipient_id, category, package):
        self.sender_id = sender_id
        self.recipient_id = recipient_id
        try:
            if isinstance(category, str):
                category = MailCategory(category)
            if isinstance(category, MailCategory):
                self.category = category
            else:
                raise ValueError(
                    f"Invalid request title. Valid titles are" f" {list(MailCategory)}"
                )
        except Exception as e:
            raise ValueError(
                f"Invalid request title. Valid titles are "
                f"{list(MailCategory)}, Error: {e}"
            ) from e
        self.package = package


class StartMail(Node):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pending_outs = deque()

    def trigger(self, context, structure_id, executable_id):
        start_mail_content = {"context": context, "structure_id": structure_id}
        start_mail = BaseMail(
            sender_id=self.id_,
            recipient_id=executable_id,
            category="start",
            package=start_mail_content,
        )
        self.pending_outs.append(start_mail)


class MailTransfer(Node):
    def __init__(self):
        super().__init__()
        self.pending_ins = {}
        self.pending_outs = deque()


@dataclass
class MailBox:

    pile: dict[str, Mail] = Field(
        default_factory=dict, description="The pile of all mails - {mail_id: Mail}"
    )

    sequence_in: dict[str, deque] = Field(
        default_factory=dict,
        description="The sequence of all incoming mails - {sender_id: deque[mail_id]}",
    )

    sequence_out: deque = Field(
        default_factory=deque,
        description="The sequence of all outgoing mails - deque[mail_id]",
    )

    def __str__(self) -> str:
        """
        Returns a string representation of the MailBox instance.

        Returns:
            str: A string describing the number of pending incoming and
                outgoing mails in the MailBox.
        """
        return (
            f"MailBox with {len(self.receieving)} pending incoming mails and "
            f"{len(self.sending)} pending outgoing mails."
        )
