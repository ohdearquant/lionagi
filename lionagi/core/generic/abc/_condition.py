from abc import ABC, abstractmethod
from pydantic import Field
from ._component import Component


class BaseCondition(ABC):
    """a situation that leads to a decision"""


class Condition(Component, BaseCondition):
    source_type: str = Field(..., description="The type of source for the condition.")

    class Config:
        """Model configuration settings."""
        extra = "allow"

    @abstractmethod
    async def __call__(self, executable) -> bool:
        pass


class Rule(Component, BaseCondition):
    """represents a rule for an item"""

    @abstractmethod
    async def applies_to(self, *args, **kwargs) -> bool:
        pass

    @abstractmethod
    async def apply(self, *args, **kwargs) -> bool:
        pass