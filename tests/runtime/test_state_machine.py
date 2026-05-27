# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.runtime.state_machine.

Covers:
- Basic transition mechanics
- Guard evaluation
- Action side effects
- History recording
- Query helpers (can_trigger, available_triggers)
- Reset semantics
- StateMachineDefinition validation and create
- Edge cases: self-transitions, multiple guards, thread safety
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from lionagi.runtime.state_machine import (
    HistoryEntry,
    StateMachine,
    StateMachineDefinition,
    StateMachineError,
    Transition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_machine(
    *,
    extra: list[Transition] | None = None,
) -> StateMachine:
    """Three-state machine: idle -> active -> done (via start / finish)."""
    transitions = [
        Transition("idle", "active", "start"),
        Transition("active", "done", "finish"),
    ]
    if extra:
        transitions.extend(extra)
    return StateMachine("test", initial_state="idle", transitions=transitions)


# ---------------------------------------------------------------------------
# 1. Basic transition
# ---------------------------------------------------------------------------


def test_basic_transition_idle_to_active() -> None:
    sm = _simple_machine()
    result = sm.trigger("start")
    assert result == "active"
    assert sm.state == "active"


def test_basic_transition_chain() -> None:
    sm = _simple_machine()
    sm.trigger("start")
    sm.trigger("finish")
    assert sm.state == "done"


def test_initial_state_unchanged_before_trigger() -> None:
    sm = _simple_machine()
    assert sm.state == "idle"


# ---------------------------------------------------------------------------
# 2. Invalid transition raises StateMachineError
# ---------------------------------------------------------------------------


def test_invalid_trigger_raises() -> None:
    sm = _simple_machine()
    with pytest.raises(StateMachineError) as exc_info:
        sm.trigger("finish")  # can't go idle -> done
    err = exc_info.value
    assert err.machine_name == "test"
    assert err.current_state == "idle"
    assert err.trigger == "finish"


def test_unknown_trigger_raises() -> None:
    sm = _simple_machine()
    with pytest.raises(StateMachineError):
        sm.trigger("nonexistent")


def test_trigger_on_terminal_raises() -> None:
    sm = _simple_machine()
    sm.trigger("start")
    sm.trigger("finish")
    with pytest.raises(StateMachineError):
        sm.trigger("start")  # done has no outgoing transitions


# ---------------------------------------------------------------------------
# 3. Guard functions
# ---------------------------------------------------------------------------


def test_guard_allows_transition() -> None:
    allowed = True

    def guard(from_state: str, to_state: str, **_kw: object) -> bool:
        return allowed

    sm = StateMachine(
        "guarded",
        initial_state="a",
        transitions=[Transition("a", "b", "go", guard=guard)],
    )
    sm.trigger("go")
    assert sm.state == "b"


def test_guard_blocks_transition() -> None:
    def guard(_fs: str, _ts: str, **_kw: object) -> bool:
        return False

    sm = StateMachine(
        "blocked",
        initial_state="a",
        transitions=[Transition("a", "b", "go", guard=guard)],
    )
    with pytest.raises(StateMachineError):
        sm.trigger("go")
    # State must not change
    assert sm.state == "a"


def test_guard_receives_context() -> None:
    received: dict[str, object] = {}

    def guard(from_state: str, to_state: str, **ctx: object) -> bool:
        received.update(ctx)
        return True

    sm = StateMachine(
        "ctx_test",
        initial_state="a",
        transitions=[Transition("a", "b", "go", guard=guard)],
    )
    sm.trigger("go", user="alice", level=3)
    assert received["user"] == "alice"
    assert received["level"] == 3


def test_first_passing_guard_wins_when_multiple_candidates() -> None:
    """Two transitions with the same trigger; first guard blocks, second passes."""

    def guard_false(_fs: str, _ts: str, **_kw: object) -> bool:
        return False

    sm = StateMachine(
        "multi",
        initial_state="a",
        transitions=[
            Transition("a", "b", "go", guard=guard_false),
            Transition("a", "c", "go"),  # no guard — always passes
        ],
    )
    sm.trigger("go")
    assert sm.state == "c"


# ---------------------------------------------------------------------------
# 4. Action functions
# ---------------------------------------------------------------------------


def test_action_called_on_transition() -> None:
    called: list[tuple[str, str]] = []

    def action(from_state: str, to_state: str, **_kw: object) -> None:
        called.append((from_state, to_state))

    sm = StateMachine(
        "action_test",
        initial_state="a",
        transitions=[Transition("a", "b", "go", action=action)],
    )
    sm.trigger("go")
    assert called == [("a", "b")]


def test_action_receives_context() -> None:
    context_seen: dict[str, object] = {}

    def action(_fs: str, _ts: str, **ctx: object) -> None:
        context_seen.update(ctx)

    sm = StateMachine(
        "action_ctx",
        initial_state="a",
        transitions=[Transition("a", "b", "go", action=action)],
    )
    sm.trigger("go", payload="hello")
    assert context_seen["payload"] == "hello"


def test_action_exception_leaves_state_unchanged() -> None:
    def bad_action(_fs: str, _ts: str, **_kw: object) -> None:
        raise RuntimeError("boom")

    sm = StateMachine(
        "bad_action",
        initial_state="a",
        transitions=[Transition("a", "b", "go", action=bad_action)],
    )
    with pytest.raises(RuntimeError, match="boom"):
        sm.trigger("go")
    assert sm.state == "a"  # must be rolled back


# ---------------------------------------------------------------------------
# 5. History tracking
# ---------------------------------------------------------------------------


def test_history_empty_initially() -> None:
    sm = _simple_machine()
    assert sm.history == []


def test_history_records_transitions() -> None:
    sm = _simple_machine()
    before = time.time()
    sm.trigger("start")
    sm.trigger("finish")
    after = time.time()

    h = sm.history
    assert len(h) == 2

    assert h[0].from_state == "idle"
    assert h[0].trigger == "start"
    assert h[0].to_state == "active"
    assert before <= h[0].timestamp <= after

    assert h[1].from_state == "active"
    assert h[1].trigger == "finish"
    assert h[1].to_state == "done"


def test_history_returns_copy() -> None:
    sm = _simple_machine()
    sm.trigger("start")
    h1 = sm.history
    sm.trigger("finish")
    h2 = sm.history
    # h1 should still have length 1
    assert len(h1) == 1
    assert len(h2) == 2


# ---------------------------------------------------------------------------
# 6. can_trigger / available_triggers
# ---------------------------------------------------------------------------


def test_can_trigger_true_for_valid_event() -> None:
    sm = _simple_machine()
    assert sm.can_trigger("start") is True


def test_can_trigger_false_for_invalid_event() -> None:
    sm = _simple_machine()
    assert sm.can_trigger("finish") is False


def test_available_triggers_returns_current_options() -> None:
    sm = _simple_machine()
    assert "start" in sm.available_triggers()
    assert "finish" not in sm.available_triggers()

    sm.trigger("start")
    assert "finish" in sm.available_triggers()
    assert "start" not in sm.available_triggers()


def test_available_triggers_no_duplicates() -> None:
    sm = StateMachine(
        "dup",
        initial_state="a",
        transitions=[
            Transition("a", "b", "go"),
            Transition("a", "c", "go"),
        ],
    )
    triggers = sm.available_triggers()
    assert triggers.count("go") == 1


# ---------------------------------------------------------------------------
# 7. Reset
# ---------------------------------------------------------------------------


def test_reset_returns_to_initial() -> None:
    sm = _simple_machine()
    sm.trigger("start")
    sm.reset()
    assert sm.state == "idle"


def test_reset_clears_history() -> None:
    sm = _simple_machine()
    sm.trigger("start")
    sm.reset()
    assert sm.history == []


def test_can_trigger_again_after_reset() -> None:
    sm = _simple_machine()
    sm.trigger("start")
    sm.trigger("finish")
    sm.reset()
    sm.trigger("start")
    assert sm.state == "active"


# ---------------------------------------------------------------------------
# 8. StateMachineDefinition.validate
# ---------------------------------------------------------------------------


def test_definition_validate_passes_for_valid_definition() -> None:
    defn = StateMachineDefinition(
        name="ok",
        states=["a", "b"],
        initial="a",
        transitions=[Transition("a", "b", "go")],
    )
    defn.validate()  # must not raise


def test_definition_validate_rejects_invalid_initial() -> None:
    defn = StateMachineDefinition(
        name="bad_init",
        states=["a", "b"],
        initial="x",  # not in states
        transitions=[Transition("a", "b", "go")],
    )
    with pytest.raises(ValueError, match="initial state"):
        defn.validate()


def test_definition_validate_rejects_bad_from_state() -> None:
    defn = StateMachineDefinition(
        name="bad_from",
        states=["a", "b"],
        initial="a",
        transitions=[Transition("z", "b", "go")],  # z not in states
    )
    with pytest.raises(ValueError, match="from_state"):
        defn.validate()


def test_definition_validate_rejects_bad_to_state() -> None:
    defn = StateMachineDefinition(
        name="bad_to",
        states=["a", "b"],
        initial="a",
        transitions=[Transition("a", "z", "go")],  # z not in states
    )
    with pytest.raises(ValueError, match="to_state"):
        defn.validate()


def test_definition_create_returns_state_machine() -> None:
    defn = StateMachineDefinition(
        name="create_test",
        states=["a", "b"],
        initial="a",
        transitions=[Transition("a", "b", "go")],
    )
    sm = defn.create()
    assert isinstance(sm, StateMachine)
    assert sm.state == "a"
    sm.trigger("go")
    assert sm.state == "b"


# ---------------------------------------------------------------------------
# 9. Edge cases
# ---------------------------------------------------------------------------


def test_self_transition() -> None:
    """A state can loop back to itself."""
    sm = StateMachine(
        "self_loop",
        initial_state="running",
        transitions=[
            Transition("running", "running", "ping"),
        ],
    )
    sm.trigger("ping")
    assert sm.state == "running"
    h = sm.history
    assert len(h) == 1
    assert h[0].from_state == "running"
    assert h[0].to_state == "running"


def test_multiple_guards_all_evaluated_in_order() -> None:
    """Three candidates; first two fail, third passes."""
    order: list[int] = []

    def g1(_fs: str, _ts: str, **_kw: object) -> bool:
        order.append(1)
        return False

    def g2(_fs: str, _ts: str, **_kw: object) -> bool:
        order.append(2)
        return False

    def g3(_fs: str, _ts: str, **_kw: object) -> bool:
        order.append(3)
        return True

    sm = StateMachine(
        "guard_order",
        initial_state="a",
        transitions=[
            Transition("a", "x", "go", guard=g1),
            Transition("a", "y", "go", guard=g2),
            Transition("a", "z", "go", guard=g3),
        ],
    )
    sm.trigger("go")
    assert sm.state == "z"
    assert order == [1, 2, 3]


def test_thread_safety_concurrent_triggers() -> None:
    """Multiple threads triggering independent machine instances do not corrupt state."""
    # Use an inline multi-step definition to exercise the full locking path.
    _LIFECYCLE = StateMachineDefinition(
        name="thread_test",
        states=["idle", "starting", "running", "stopping", "stopped", "failed"],
        initial="idle",
        transitions=[
            Transition("idle", "starting", "start"),
            Transition("starting", "running", "started"),
            Transition("running", "stopping", "stop"),
            Transition("stopping", "stopped", "stopped"),
            Transition("starting", "failed", "fail"),
            Transition("running", "failed", "fail"),
            Transition("stopped", "idle", "reset"),
            Transition("failed", "idle", "reset"),
        ],
    )

    errors: list[BaseException] = []
    results: list[str] = []
    lock = threading.Lock()

    def run_lifecycle() -> None:
        try:
            sm = _LIFECYCLE.create()
            sm.trigger("start")
            sm.trigger("started")
            sm.trigger("stop")
            sm.trigger("stopped")
            with lock:
                results.append(sm.state)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run_lifecycle) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    assert all(r == "stopped" for r in results)


def test_history_entry_is_named_tuple() -> None:
    sm = _simple_machine()
    sm.trigger("start")
    entry = sm.history[0]
    assert isinstance(entry, HistoryEntry)
    assert entry.from_state == "idle"
    assert entry.trigger == "start"
    assert entry.to_state == "active"
    assert isinstance(entry.timestamp, float)


def test_machine_repr() -> None:
    sm = _simple_machine()
    r = repr(sm)
    assert "test" in r
    assert "idle" in r
