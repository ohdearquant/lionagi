from abc import ABC
from ._component import Component


class Ordering(Component, ABC):
    """represents a sequence of items' id in a specific order"""

    sequence: any = None

    def __len__(self) -> int:
        return len(self.sequence)

    def __iter__(self):
        return iter(self.sequence)
    