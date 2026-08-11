"""Pile and Progression are iterables, not iterators.

Traversal position belongs to the object ``iter(container)`` returns, never to
the container. These tests pin that split: the container refuses ``next()``,
and an iterator taken from it advances, exhausts, and stays independent of any
other iterator over the same container.
"""

import copy
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator

import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression


@pytest.fixture
def elements():
    return [Element() for _ in range(4)]


@pytest.fixture
def pile(elements):
    return Pile(collections=elements)


@pytest.fixture
def progression(elements):
    return Progression(order=[e.id for e in elements])


# The container is not an iterator


def test_next_on_pile_raises_type_error(pile):
    with pytest.raises(TypeError):
        next(pile)


def test_next_on_progression_raises_type_error(progression):
    with pytest.raises(TypeError):
        next(progression)


@pytest.mark.asyncio
async def test_anext_on_pile_raises_type_error(pile):
    with pytest.raises(TypeError):
        await anext(pile)


def test_pile_is_iterable_but_not_iterator(pile):
    assert isinstance(pile, Iterable)
    assert not isinstance(pile, Iterator)
    assert isinstance(pile, AsyncIterable)
    assert not isinstance(pile, AsyncIterator)


def test_progression_is_iterable_but_not_iterator(progression):
    assert isinstance(progression, Iterable)
    assert not isinstance(progression, Iterator)


def test_iter_returns_a_distinct_object(pile, progression):
    assert iter(pile) is not pile
    assert iter(progression) is not progression


# An iterator taken from the container behaves like one


def test_repeated_next_on_pile_iterator_advances(pile, elements):
    it = iter(pile)
    assert [next(it) for _ in range(4)] == elements
    with pytest.raises(StopIteration):
        next(it)


def test_repeated_next_on_progression_iterator_advances(progression, elements):
    it = iter(progression)
    assert [next(it) for _ in range(4)] == [e.id for e in elements]
    with pytest.raises(StopIteration):
        next(it)


def test_two_pile_iterators_are_independent(pile, elements):
    a, b = iter(pile), iter(pile)
    assert next(a) is elements[0]
    assert next(a) is elements[1]
    # b has its own cursor and still starts at the beginning.
    assert next(b) is elements[0]


def test_two_progression_iterators_are_independent(progression, elements):
    a, b = iter(progression), iter(progression)
    next(a)
    assert next(a) == elements[1].id
    assert next(b) == elements[0].id


def test_copy_does_not_inherit_a_traversal_position(pile, elements):
    it = iter(pile)
    next(it)
    next(it)
    # The container carries no cursor, so a copy has nothing to resume from.
    assert list(copy.copy(pile)) == elements
    assert list(copy.deepcopy(pile)) == [pile.collections[e.id] for e in elements]


# Ordinary traversal is unchanged


def test_for_loop_over_pile(pile, elements):
    assert [item for item in pile] == elements


def test_for_loop_restarts_each_time(pile, elements):
    assert [item for item in pile] == elements
    assert [item for item in pile] == elements


def test_for_loop_over_progression(progression, elements):
    assert [key for key in progression] == [e.id for e in elements]


@pytest.mark.asyncio
async def test_async_for_over_pile(pile, elements):
    collected = [item async for item in pile]
    assert collected == elements


@pytest.mark.asyncio
async def test_async_pile_iterator_is_a_real_async_iterator(pile, elements):
    it = Pile.AsyncPileIterator(pile)
    assert it.__aiter__() is it
    assert [await anext(it) for _ in range(4)] == elements
    with pytest.raises(StopAsyncIteration):
        await anext(it)
