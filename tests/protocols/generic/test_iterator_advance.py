# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from uuid import uuid4

import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression


def test_progression_next_advances():
    a, b = uuid4(), uuid4()
    prog = Progression(order=[a, b])

    assert next(prog) == a
    assert next(prog) == b
    with pytest.raises(StopIteration):
        next(prog)


def test_progression_next_restarts_after_exhaustion():
    a, b = uuid4(), uuid4()
    prog = Progression(order=[a, b])

    assert next(prog) == a
    assert next(prog) == b
    with pytest.raises(StopIteration):
        next(prog)
    # A fresh round of direct next() calls should work again.
    assert next(prog) == a
    assert next(prog) == b


def test_pile_next_advances():
    e1, e2 = Element(), Element()
    pile = Pile(collections=[e1, e2])

    assert next(pile) is e1
    assert next(pile) is e2
    with pytest.raises(StopIteration):
        next(pile)
