# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from uuid import uuid4

import pytest

from lionagi._errors import ItemExistsError, ItemNotFoundError
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.flow import Flow
from lionagi.protocols.generic.progression import Progression

# ---------------------------------------------------------------------------
# Direct Progression.order mutation must keep the membership cache in sync
# ---------------------------------------------------------------------------


def test_direct_order_append_updates_membership():
    a, b = uuid4(), uuid4()
    prog = Progression(order=[a])

    prog.order.append(b)

    assert len(prog) == 2
    assert b in prog


def test_direct_order_clear_updates_membership():
    a = uuid4()
    prog = Progression(order=[a])

    prog.order.clear()

    assert len(prog) == 0
    assert a not in prog


def test_direct_order_remove_and_delitem_update_membership():
    a, b, c = uuid4(), uuid4(), uuid4()
    prog = Progression(order=[a, b, c])

    prog.order.remove(b)
    assert b not in prog
    assert len(prog) == 2

    del prog.order[0]
    assert a not in prog
    assert len(prog) == 1


def test_direct_order_setitem_updates_membership():
    a, b, c = uuid4(), uuid4(), uuid4()
    prog = Progression(order=[a, b])

    prog.order[0] = c

    assert a not in prog
    assert c in prog
    assert len(prog) == 2


# ---------------------------------------------------------------------------
# Flow: renaming an owned progression must keep the name index consistent
# ---------------------------------------------------------------------------


def test_flow_rename_progression_updates_index():
    item = Element()
    flow = Flow()
    flow.add_item(item)
    prog = Progression(order=[item.id], name="before")
    flow.add_progression(prog)

    flow.rename_progression("before", "after")

    with pytest.raises(ItemNotFoundError):
        flow.get_progression("before")
    assert flow.get_progression("after") is prog


def test_flow_rename_progression_rejects_existing_name():
    item = Element()
    flow = Flow()
    flow.add_item(item)
    prog_a = Progression(order=[item.id], name="a")
    prog_b = Progression(order=[item.id], name="b")
    flow.add_progression(prog_a)
    flow.add_progression(prog_b)

    with pytest.raises(ItemExistsError):
        flow.rename_progression("a", "b")


def test_flow_remove_progression_by_uuid_clears_stale_name_index():
    item = Element()
    flow = Flow()
    flow.add_item(item)
    prog = Progression(order=[item.id], name="before")
    flow.add_progression(prog)

    flow.rename_progression("before", "after")
    flow.remove_progression(prog.id)

    assert flow._progression_names == {}
    assert flow._progression_ids == {}
    with pytest.raises(ItemNotFoundError):
        flow.get_progression("after")
