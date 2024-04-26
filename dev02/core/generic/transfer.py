from collections import deque
from pydantic import Field
from pydantic.dataclasses import dataclass

from lionagi.core.generic.mail import Mail
from lionagi.core.generic.signal import Signal


@dataclass
class Transfer:

    schedule: dict[str, deque[Mail | Signal]] = Field(
        default_factory=dict,
        description="The sequence of all pending mails - {direction: deque[mail_id]}",
    )

    @property
    def is_empty(self) -> bool:
        """Returns a flag indicating whether the transfer is empty."""
        return not any(self.schedule.values())
