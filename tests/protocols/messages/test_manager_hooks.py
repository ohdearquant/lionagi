# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Hook contract regression tests for MessageManager: snapshot semantics, failure aggregation, and re-entrant safety."""

from __future__ import annotations

import sys

import pytest

from lionagi.protocols.types import MessageManager

# Use the same conditional backport selector the manager uses. On 3.11+
# we get the builtin; on 3.10 we get the exceptiongroup package's class
# (anyio/pytest pull it in transitively).
if sys.version_info >= (3, 11):
    _ExcGroup = BaseExceptionGroup  # noqa: F821
else:
    from exceptiongroup import BaseExceptionGroup as _ExcGroup


@pytest.fixture
def mm() -> MessageManager:
    return MessageManager()


async def test_hook_added_during_iteration_does_not_fire_this_call(
    mm: MessageManager,
):
    """A hook that appends another callback during iteration must NOT see that new callback fire for the current message; the snapshot decouples iteration from public mutations."""
    fired_outer: list = []
    fired_late: list = []

    async def late_hook(msg):
        fired_late.append(msg)

    async def outer_hook(msg):
        fired_outer.append(msg)
        # Append a NEW hook to the public list during iteration.
        mm._on_message_added.append(late_hook)

    mm._on_message_added.append(outer_hook)

    msg1 = await mm.a_add_message(
        instruction="first",
        sender="u",
        recipient="x",
    )

    # outer_hook saw msg1; late_hook did NOT.
    assert fired_outer == [msg1]
    assert fired_late == []

    # On the next message, both fire (late_hook is now in the snapshot).
    msg2 = await mm.a_add_message(
        instruction="second",
        sender="u",
        recipient="x",
    )
    assert fired_outer == [msg1, msg2]
    assert fired_late == [msg2]


async def test_hook_removed_during_iteration_still_fires_this_call(
    mm: MessageManager,
):
    """A hook that removes itself while iterating still fires for the current message (snapshot held it); gone on the next message."""
    fired: list = []

    async def self_removing_hook(msg):
        fired.append(msg)
        mm._on_message_added.remove(self_removing_hook)

    mm._on_message_added.append(self_removing_hook)

    msg1 = await mm.a_add_message(
        instruction="a",
        sender="u",
        recipient="x",
    )
    await mm.a_add_message(
        instruction="b",
        sender="u",
        recipient="x",
    )

    # Hook fired exactly once: for msg1 (then removed itself before msg2).
    assert fired == [msg1]


def test_sync_add_message_snapshot_isolated_from_public_mutation(
    mm: MessageManager,
):
    """The sync path also snapshots before iterating. A sync hook
    appending another sync hook does not inject into the current fire.
    """
    fired_outer: list = []
    fired_late: list = []

    def late(msg):
        fired_late.append(msg)

    def outer(msg):
        fired_outer.append(msg)
        mm._on_message_added.append(late)

    mm._on_message_added.append(outer)

    msg1 = mm.add_message(instruction="x", sender="u", recipient="r")
    assert fired_outer == [msg1]
    assert fired_late == []

    msg2 = mm.add_message(instruction="y", sender="u", recipient="r")
    assert fired_late == [msg2]


async def test_one_failing_hook_does_not_prevent_others_async(
    mm: MessageManager,
):
    fired_b: list = []
    fired_c: list = []

    async def hook_a(msg):
        raise RuntimeError("a failed")

    async def hook_b(msg):
        fired_b.append(msg)

    async def hook_c(msg):
        fired_c.append(msg)

    mm._on_message_added.extend([hook_a, hook_b, hook_c])

    with pytest.raises(RuntimeError, match="a failed"):
        await mm.a_add_message(
            instruction="hello",
            sender="u",
            recipient="x",
        )

    # b and c still fired despite a raising.
    assert len(fired_b) == 1
    assert len(fired_c) == 1


async def test_multiple_failing_hooks_aggregated_into_exception_group(
    mm: MessageManager,
):
    """When >1 hooks fail, the errors are surfaced as a single
    ``BaseExceptionGroup`` (3.10 backport) so callers can inspect all
    failures, not just the first.
    """

    async def hook_a(msg):
        raise RuntimeError("first")

    async def hook_b(msg):
        raise ValueError("second")

    async def hook_c(msg):
        return  # succeeds

    mm._on_message_added.extend([hook_a, hook_b, hook_c])

    with pytest.raises(_ExcGroup) as excinfo:
        await mm.a_add_message(
            instruction="hi",
            sender="u",
            recipient="x",
        )

    excs = excinfo.value.exceptions
    assert len(excs) == 2
    types = sorted(type(e).__name__ for e in excs)
    assert types == ["RuntimeError", "ValueError"]


def test_one_failing_sync_hook_does_not_prevent_others(mm: MessageManager):
    """Sync hooks also use the collect-then-raise pattern."""
    fired_b: list = []

    def hook_a(msg):
        raise RuntimeError("a failed")

    def hook_b(msg):
        fired_b.append(msg)

    mm._on_message_added.extend([hook_a, hook_b])

    with pytest.raises(RuntimeError, match="a failed"):
        mm.add_message(instruction="x", sender="u", recipient="x")

    assert len(fired_b) == 1


def test_sync_preflight_rejects_async_hook_before_pile_mutation(
    mm: MessageManager,
):
    """R4-A MED-1 regression guard (also covered in test_manager_state.py).
    Pinned here as part of the hooks contract suite.
    """

    async def async_hook(_msg):  # pragma: no cover — never invoked
        return None

    mm._on_message_added.append(async_hook)
    msgs_before = len(mm.messages)

    with pytest.raises(RuntimeError, match="Async on_message_added"):
        mm.add_message(instruction="hi", sender="u", recipient="x")

    # Critical: pile was NOT mutated.
    assert len(mm.messages) == msgs_before


async def test_a_add_message_safe_when_hook_calls_a_add_message(
    mm: MessageManager,
):
    """A hook calling a_add_message re-entrantly must not deadlock or double-fire; outer and inner calls use independent snapshots."""
    fired: list = []

    async def echo_hook(msg):
        fired.append(("outer", msg))
        # Re-entrant call: if this was the original instruction, emit
        # a derived "echo" — but only once, to avoid infinite recursion.
        from lionagi.protocols.messages import Instruction

        if isinstance(msg, Instruction) and msg.content.instruction == "trigger":
            await mm.a_add_message(
                instruction="echo",
                sender="hook",
                recipient="x",
            )

    mm._on_message_added.append(echo_hook)

    await mm.a_add_message(
        instruction="trigger",
        sender="u",
        recipient="x",
    )

    # Hook fired for BOTH the trigger AND the echo it added.
    assert len(fired) == 2
    # Both messages landed in the pile.
    contents = [m.content.instruction for _, m in fired]
    assert "trigger" in contents
    assert "echo" in contents
