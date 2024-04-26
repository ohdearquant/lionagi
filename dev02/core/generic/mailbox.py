from collections import deque
from pydantic import Field
from pydantic.dataclasses import dataclass

from lionagi.core.generic.mail import Mail


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
