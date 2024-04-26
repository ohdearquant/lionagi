from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable
from pydantic import BaseModel, Field


class ConditionSource(str, Enum):
    """
    Enumeration for specifying the source type of a condition.

    Attributes:
        STRUCTURE: Represents a condition based on the structure.
        EXECUTABLE: Represents a condition that can be executed or evaluated.
    """

    STRUCTURE = "structure"
    EXECUTABLE = "executable"
    RULE = "rule"


class Condition(BaseModel, ABC):
    """
    Abstract base class for defining conditions associated with edges.

    Attributes:
        source_type (ConditionSource): Specifies the type of source for the condition.

    Methods:
        check: Abstract method that should be implemented to evaluate the condition.
    """

    source_type: str = Field(..., description="The type of source for the condition.")

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