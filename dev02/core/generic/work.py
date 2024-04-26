from pydantic.dataclasses import dataclass
from typing import Any
from abc import ABC, abstractmethod
from pydantic import Field

from .node import Node


@dataclass
class Work:

    work_completed: bool = Field(
        default=False,
        description="A flag indicating whether the work is already completed.",
    )

    work_result: Any | None = Field(
        default=None,
        description="The result of the work.",
    )

    context: dict | str | None = Field(
        None, description="The context buffer for the next instruction."
    )


class Worker(Node, ABC):
    stopped: bool = False

    @abstractmethod
    def perform(self, *args, **kwargs):
        pass

    @abstractmethod
    def forward(self, *args, **kwargs):
        pass

    def stop(self):
        self.stopped = True
        return True
