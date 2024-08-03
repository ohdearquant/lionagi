from functools import singledispatch


from typing import Any

from lion_core.generic import (
    Progression as CoreProgression,
    Pile as CorePile,
    Note as CoreNote,
)

from lionagi.os.primitives.container.progression import Progression
from lionagi.os.primitives.container.pile import Pile
from lionagi.os.primitives.container.note import Note


@singledispatch
def core_to_lionagi_container(input_: Any):
    return input_


@core_to_lionagi_container.register(CoreProgression)
def _(input_: CoreProgression):
    if isinstance(input_, Progression):
        return input_
    return Progression(list(input_), name=input_.name)


@core_to_lionagi_container.register(CorePile)
def _(input_: CorePile):
    if isinstance(input_, Pile):
        return input_
    return Pile(
        [core_to_lionagi_container(item) for item in input_],
        item_type=input_.item_type,
        strict=input_.strict,
    )


@core_to_lionagi_container.register(CoreNote)
def _(input_: CoreNote):
    if isinstance(input_, Note):
        return input_
    return Note(**input_.to_dict())


@core_to_lionagi_container.register(list)
def _(input_: list):
    return [core_to_lionagi_container(item) for item in input_]
