# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

E = TypeVar("E")


__all__ = (
    "Observer",
    "Manager",
    "Relational",
    "Sendable",
    "PileItem",
    "Communicatable",
    "Condition",
    "Collective",
    "Ordering",
    "Composable",
    "Composed",
)


class Observer(ABC):  # noqa: B024
    """Base for all observers."""

    pass


class Manager(Observer):
    """Base for all managers."""

    pass


class Relational(ABC):  # noqa: B024
    """Base for graph-connectable objects."""

    pass


class Sendable(ABC):  # noqa: B024
    """Sendable entities must define 'sender' and 'recipient'."""

    pass


class PileItem(ABC):  # noqa: B024
    """Pile-item admission contract. Nominal: requires inheritance, not a bare ``id`` attribute."""

    pass


class Composable(ABC):  # noqa: B024
    """A item that can be composed into a composed entity."""


class Composed(ABC):
    @classmethod
    @abstractmethod
    def compose(cls, members: tuple[Composable, ...]):
        """Compose from components."""


class Communicatable(PileItem):
    """Communicatable must define 'mailbox' and send/receive methods."""

    @abstractmethod
    def send(self, *args, **kwargs):
        pass


class Condition(ABC):
    """Base for conditions."""

    @abstractmethod
    async def apply(self, *args, **kwargs) -> bool:
        pass


class Collective(ABC, Generic[E]):
    """Base for collections of elements."""

    @abstractmethod
    def include(self, item, /):
        pass

    @abstractmethod
    def exclude(self, item, /):
        pass


class Ordering(ABC, Generic[E]):
    """Base for element orderings."""

    @abstractmethod
    def include(self, item, /):
        pass

    @abstractmethod
    def exclude(self, item, /):
        pass
