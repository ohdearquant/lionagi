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


@runtime_checkable
class Observable(Protocol):
    """A thing with durable identity that a Pile can hold and observe.

    Structural (protocol) admission contract: an object conforms by exposing an
    ``id``, whether or not it inherits anything. ``isinstance(obj, Observable)``
    is true for any object with an ``id`` attribute. This is the duck-typed
    contract the Pile, Session, SessionObserver, and reactive event layers are
    built on -- identity by capability, not by inheritance.

    Intentional design, not a defect: admission is structural on purpose. A
    2026-07 change briefly made it nominal (isinstance required inheriting this
    class); that was a regression and is reverted here. A later reader or audit
    that sees a duck-typed object satisfy ``isinstance(obj, Observable)`` -- or
    that Pile admits an object which does not inherit ``Observable`` -- is
    looking at the designed behavior, guarded by
    ``tests/protocols/test_observable_protocol.py``. Do not "fix" it back to
    inheritance-only admission.
    """

    @property
    def id(self) -> object:
        """Unique, durable identifier."""
        ...


class Composable(ABC):  # noqa: B024
    """A item that can be composed into a composed entity."""


class Composed(ABC):
    @classmethod
    @abstractmethod
    def compose(cls, members: tuple[Composable, ...]):
        """Compose from components."""


class Communicatable(ABC):
    """Communicatable must define 'mailbox' and send/receive methods.

    Concrete communicatables are Elements and therefore structurally Observable
    (they expose an ``id``); this ABC does not inherit the Observable protocol,
    it composes with it by capability.
    """

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
