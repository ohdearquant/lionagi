from pydantic import Field
from collections import deque
from typing import TypeVar
from .abc import Ordering, Component

T = TypeVar("T")


class SequentialOrdering(Ordering):
    """the ordering cannot be changed after joining the sequence"""

    sequence: deque = Field(
        default_factory=deque,
        description="The sequence of lionagi items' id in the order.",
    )

    def append(self, item: T | str):
        """Appends an item to the sequence."""
        self.sequence.append(item.id_ if isinstance(item, Component) else item)


class CategorizedOrdering(Ordering):
    """multi-sequence of items with different category keys"""

    sequence: dict[str, SequentialOrdering] = Field(
        default_factory=dict,
        description="The multi-sequence of lionagi items' id in the order.",
    )
    
    def append(self, key: str, item: T | str):
        """Appends an item to the specified sequence category."""
        if not key in self.sequence:
            self.sequence[key] = SequentialOrdering()
        self.sequence[key].append(item)
        
    def __getitem__(self, key: str) -> deque | None:
        """Retrieves the sequence for the specified category key."""
        return self.sequence.get(key, None)