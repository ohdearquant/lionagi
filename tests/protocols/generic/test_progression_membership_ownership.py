# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Membership-cache ownership must be keyed by identity, not by type.

`Progression` keeps a `_MembersDeque` wrapper so that `x in progression` is
O(1). Deciding which `Progression` owns a given wrapper by type and length
(``isinstance`` plus a size check) is not enough, because a single wrapper can
end up referenced by two instances -- through `model_copy()`,
`Progression.model_construct(order=other.order)`, cross-instance field
assignment, or a `deque.copy()` of the wrapper assigned elsewhere. Once that
happens, a length-preserving mutation through one instance leaves the other
reporting membership that no longer matches its own `order`, and no length
check can see it. Ownership is therefore checked by identity
(``order._members_ref is self._members``); these tests pin that.

The final test sweeps `dir(deque)` and requires every mutating method to be
either overridden by `_MembersDeque` or on an explicit allowlist of read-only,
id-set-preserving methods, so that a newly missed mutator fails the suite
rather than depending on someone noticing it.
"""

from __future__ import annotations

from collections import deque
from uuid import uuid4

from lionagi.protocols.generic.progression import Progression, _MembersDeque

# Ownership-by-identity: sharing a `_MembersDeque` across two `Progression`
# instances must never let a length-preserving mutation through one instance
# corrupt the other's membership view.


def test_model_copy_shared_wrapper_both_instances_correct_after_setitem():
    p = Progression(order=[uuid4() for _ in range(3)])
    q = p.model_copy()
    assert q.order is p.order  # documented pydantic shallow-copy behavior

    # A plain shallow copy shares `order` *and* `_members` (same objects), so
    # it is self-consistent on its own. The narrowing surfaces once anything
    # forces an independent resync: growing the order through `q` advances
    # only `q._order_len`, so a subsequent read through `p` finds
    # `len(order) != p._order_len` and silently rebuilds+rebinds the *shared*
    # wrapper onto `p`'s fresh `_members`, orphaning `q`'s cache.
    extra = uuid4()
    q.append(extra)
    assert extra in p  # triggers p._ensure_synced() -> _rebuild_members()

    old = q.order[0]
    new = uuid4()
    q.order[0] = new  # length-preserving external mutation via q

    assert old not in q
    assert new in q
    assert old not in p
    assert new in p


def test_model_construct_shared_order_both_instances_correct_after_setitem():
    p = Progression(order=[uuid4() for _ in range(3)])
    q = Progression.model_construct(order=p.order)
    assert q.order is p.order

    old = q.order[0]
    new = uuid4()
    q.order[0] = new  # length-preserving external mutation via q

    assert old not in q
    assert new in q
    assert old not in p
    assert new in p


def test_field_assignment_shared_order_both_instances_correct_after_setitem():
    a = Progression(order=[uuid4() for _ in range(3)])
    b = Progression(order=[uuid4() for _ in range(3)])
    a.order = b.order
    assert a.order is b.order

    old = a.order[0]
    new = uuid4()
    a.order[0] = new  # length-preserving external mutation via a

    assert old not in a
    assert new in a
    assert old not in b
    assert new in b


def test_deque_copy_of_members_deque_assigned_unbound_wrapper():
    src = Progression(order=[uuid4() for _ in range(3)])
    dest = Progression(order=list(src.order))
    unbound = src.order.copy()  # deque.copy() -> fresh _MembersDeque, _members_ref=None
    assert isinstance(unbound, _MembersDeque)

    dest.order = unbound  # same length as dest's current order; no rebuild triggered
    assert dest.order is unbound

    old = dest.order[0]
    new = uuid4()
    dest.order[0] = new  # length-preserving mutation on an unbound wrapper

    assert old not in dest
    assert new in dest
    # src's own wrapper/cache were never touched and must stay fully correct.
    assert src.order[0] in src


# Exhaustive accounting of every `deque` mutator.


def test_dir_deque_mutators_all_overridden_or_allowlisted():
    overridden = {name for name, value in vars(_MembersDeque).items() if callable(value)}

    # Reviewed, id-set-preserving or read-only `deque` surface that never
    # needs to touch the bound membership set: permutation (rotate, reverse),
    # non-mutating construction/copy (`__add__`/`__mul__`/`__rmul__`/`copy`),
    # and plain read/dunder machinery.
    non_mutating_or_id_preserving_allowlist = {
        "rotate",
        "reverse",
        "copy",
        "count",
        "index",
        "maxlen",
        "__add__",
        "__mul__",
        "__rmul__",
        "__bool__",
        "__class__",
        "__copy__",
        "__deepcopy__",
        "__reduce__",
        "__reduce_ex__",
        "__sizeof__",
        "__repr__",
        "__str__",
        "__eq__",
        "__ne__",
        "__lt__",
        "__le__",
        "__gt__",
        "__ge__",
        "__hash__",
        "__len__",
        "__iter__",
        "__reversed__",
        "__contains__",
        "__getitem__",
        "__class_getitem__",
        "__init__",
        "__new__",
        "__init_subclass__",
        "__subclasshook__",
        "__dir__",
        "__doc__",
        "__format__",
        "__getattribute__",
        "__delattr__",
        "__setattr__",
        # Present in dir(deque) from Python 3.14: both read-only.
        "__getstate__",
        "__module__",
    }

    unaccounted = [
        name
        for name in dir(deque)
        if name not in overridden and name not in non_mutating_or_id_preserving_allowlist
    ]
    assert not unaccounted, (
        "deque API members neither overridden by _MembersDeque nor explicitly "
        f"allowlisted as non-mutating/id-preserving: {sorted(unaccounted)}"
    )
