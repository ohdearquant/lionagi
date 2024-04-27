from collections import deque
from abc import ABC, abstractmethod
from typing import Any

from pydantic import Field

from lionagi.core.generic import BaseComponent
from lionagi.core.mail.schema import BaseMail


class BaseExecutor(BaseComponent, ABC):
    pending_ins: dict = Field(
        default_factory=dict, description="The pending incoming mails."
    )
    pending_outs: deque = Field(
        default_factory=deque, description="The pending outgoing mails."
    )
    execute_stop: bool = Field(
        False, description="A flag indicating whether to stop execution."
    )
    context: dict | str | list | None = Field(
        None, description="The context buffer for the next instruction."
    )
    execution_responses: list = Field(
        default_factory=list, description="The list of responses."
    )
    context_log: list = Field(default_factory=list, description="The context log.")
    verbose: bool = Field(
        True, description="A flag indicating whether to provide verbose output."
    )

    def send(self, recipient_id: str, category: str, package: Any) -> None:
        """
        Sends a mail to a recipient.

        Args:
            recipient_id (str): The ID of the recipient.
            category (str): The category of the mail.
            package (Any): The package to send in the mail.
        """
        mail = BaseMail(
            sender_id=self.id_,
            recipient_id=recipient_id,
            category=category,
            package=package,
        )
        self.pending_outs.append(mail)
