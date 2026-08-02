# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
from typing import Generic, Protocol, TypeVar, runtime_checkable

E = TypeVar("E")


__all__ = (
    "Observer",
    "Manager",
    "Relational",
    "Sendable",
    "Observable",
    "Communicatable",
    "Condition",
    "Collective",
    "Ordering",
    "Composable",
    "Composed",
)


class Observer(ABC):  # noqa: B024
    pass


class Manager(Observer):
    pass


class Relational(ABC):  # noqa: B024
    pass


class Sendable(ABC):  # noqa: B024
    """Sendable entities must define 'sender' and 'recipient'."""

    pass


@runtime_checkable
class Observable(Protocol):
    """Durable-identity object a Pile can hold. Admission is structural -- any
    object exposing ``id`` satisfies ``isinstance(obj, Observable)``, inheritance
    or not; guarded by ``tests/protocols/test_observable_protocol.py``.
    """

    @property
    def id(self) -> object:
        """Unique, durable identifier."""
        ...


class Composable(ABC):  # noqa: B024
    pass


class Composed(ABC):
    @classmethod
    @abstractmethod
    def compose(cls, members: tuple[Composable, ...]):
        pass


class Communicatable(ABC):
    """Communicatable must define 'mailbox' and send/receive methods; composes with
    Observable by capability rather than inheriting it.
    """

    @abstractmethod
    def send(self, *args, **kwargs):
        pass


class Condition(ABC):
    @abstractmethod
    async def apply(self, *args, **kwargs) -> bool:
        pass


class Collective(ABC, Generic[E]):
    @abstractmethod
    def include(self, item, /):
        pass

    @abstractmethod
    def exclude(self, item, /):
        pass


class Ordering(ABC, Generic[E]):
    @abstractmethod
    def include(self, item, /):
        pass

    @abstractmethod
    def exclude(self, item, /):
        pass
