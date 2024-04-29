from collections import deque
from pydantic import Field, field_validator

from .abc import Component
from .pile import SequencedPile


class Mail(Component):

    sender: str = Field(
        default="N/A",
        title="Sender",
        description="The id of the sender.",
    )

    recipient: str = Field(
        default="N/A",
        title="Recipient",
        description="The id of the recipient",
    )

    @field_validator("sender", "recipient", mode="before")
    def _validate_sender_recipient(cls, value):
        if isinstance(value, Component):
            return value.id_
        return value


class MailBox(SequencedPile):
    
    def __init__(self):
        super().__init__(Mail)
    
    def append(self, mail: Mail, out=False):
        self._append(mail, category=None if out else mail.sender)

    def popleft(self, sender: str=None) -> Mail:
        return self.popleft(category=sender)

    @property
    def pending_out(self) -> deque:
        return self.sequence

    @property
    def pending_in(self) -> deque:
        return self.categorized_sequence.sequence
    
    def __str__(self) -> str:
        """
        Returns a string representation of the MailBox instance.

        Returns:
            str: A string describing the number of pending incoming and
                outgoing mails in the MailBox.
        """
        return (
            f"MailBox with {len(self.pending_in)} pending incoming mails and "
            f"{len(self.pending_out)} pending outgoing mails."
        )