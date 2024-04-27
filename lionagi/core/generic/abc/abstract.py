from abc import ABC, abstractmethod
from pydantic import BaseModel
from .component import BaseComponent


class Log(ABC):
    """represents a log of items"""


class Sequence(ABC):
    """represents a sequence of items"""


class Record(BaseComponent, ABC):
    """represents a storable object for an item"""


class Condition(BaseModel, ABC):

    class Config:
        """Model configuration settings."""

        extra = "allow"

    @abstractmethod
    def __call__(self, executable) -> bool:
        """Evaluates the condition based on implemented logic.

        Returns:
            bool: The boolean result of the condition evaluation.
        """
        pass