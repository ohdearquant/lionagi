from .abc import BaseComponent
from pydantic import Field, field_validator


class Mail(BaseComponent):

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
        if isinstance(value, BaseComponent):
            return value.id_
        return value
