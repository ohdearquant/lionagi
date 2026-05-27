# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Generic state machine for runtime and schedule lifecycles.

Provides a table-driven state machine that is thread-safe, auditable via a
history log, and composable through pre-built definitions for the runner and
schedule lifecycles defined in ADR-0062.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


class State(str):
    """A state name.  Plain ``str`` subclass so literals work everywhere."""

    def __repr__(self) -> str:
        return f"State({str.__repr__(self)})"


class Transition(NamedTuple):
    """One edge in the state graph.

    Attributes:
        from_state: Source state.
        to_state:   Destination state.
        trigger:    Event name that fires this edge.
        guard:      Optional callable ``(from_state, to_state, **ctx) -> bool``.
                    The transition is skipped when the guard returns ``False``.
        action:     Optional callable ``(from_state, to_state, **ctx) -> None``
                    executed after the guard passes but before the state is
                    updated.  Exceptions propagate to the caller; the state is
                    **not** updated if the action raises.
    """

    from_state: str
    to_state: str
    trigger: str
    guard: Callable[..., bool] | None = None
    action: Callable[..., None] | None = None


class HistoryEntry(NamedTuple):
    """One recorded transition in the machine history."""

    from_state: str
    trigger: str
    to_state: str
    timestamp: float


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StateMachineError(Exception):
    """Raised when no valid transition exists for the requested event.

    Attributes:
        machine_name: Name of the machine that raised the error.
        current_state: State the machine was in when the event arrived.
        trigger: The event that could not be dispatched.
    """

    def __init__(
        self,
        message: str,
        *,
        machine_name: str = "",
        current_state: str = "",
        trigger: str = "",
    ) -> None:
        super().__init__(message)
        self.machine_name = machine_name
        self.current_state = current_state
        self.trigger = trigger


# ---------------------------------------------------------------------------
# StateMachine
# ---------------------------------------------------------------------------


class StateMachine:
    """A thread-safe, table-driven finite state machine.

    Args:
        name:          Human-readable identifier used in error messages.
        initial_state: Starting state; also used by :meth:`reset`.
        transitions:   All valid edges.  Multiple transitions sharing the same
                       ``(from_state, trigger)`` pair are evaluated in list
                       order; the first one whose guard returns ``True`` (or
                       that has no guard) wins.

    Example::

        sm = StateMachine(
            "door",
            initial_state="closed",
            transitions=[
                Transition("closed", "open", "open"),
                Transition("open",   "closed", "close"),
            ],
        )
        sm.trigger("open")
        assert sm.state == "open"
    """

    def __init__(
        self,
        name: str,
        initial_state: str,
        transitions: list[Transition],
    ) -> None:
        self._name = name
        self._initial_state = initial_state
        self._state = initial_state
        self._transitions = transitions
        self._history: list[HistoryEntry] = []
        self._lock = threading.Lock()

        # Index: (from_state, trigger) -> list[Transition] for O(1) lookup.
        self._index: dict[tuple[str, str], list[Transition]] = {}
        for t in transitions:
            key = (t.from_state, t.trigger)
            self._index.setdefault(key, []).append(t)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Machine name."""
        return self._name

    @property
    def state(self) -> str:
        """Current state (thread-safe read)."""
        with self._lock:
            return self._state

    @property
    def history(self) -> list[HistoryEntry]:
        """Read-only snapshot of the transition history."""
        with self._lock:
            return list(self._history)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def trigger(self, event: str, **context: object) -> str:
        """Fire *event* and advance to the next state.

        Iterates candidates in definition order and uses the first transition
        whose guard (if any) returns ``True``.  If no candidate passes,
        :class:`StateMachineError` is raised and the machine state is
        unchanged.

        Args:
            event:     Trigger name.
            **context: Arbitrary keyword arguments forwarded to guard and
                       action callables as keyword arguments.

        Returns:
            The new state after the transition.

        Raises:
            StateMachineError: No valid transition from the current state for
                               *event* (either not defined or all guards
                               returned ``False``).
        """
        with self._lock:
            candidates = self._index.get((self._state, event), [])
            chosen: Transition | None = None
            for t in candidates:
                if t.guard is None or t.guard(t.from_state, t.to_state, **context):
                    chosen = t
                    break

            if chosen is None:
                raise StateMachineError(
                    f"[{self._name}] no valid transition for trigger "
                    f"{event!r} from state {self._state!r}",
                    machine_name=self._name,
                    current_state=self._state,
                    trigger=event,
                )

            # Run action *before* committing the state change so that if the
            # action raises, the machine stays in its previous state.
            if chosen.action is not None:
                chosen.action(chosen.from_state, chosen.to_state, **context)

            entry = HistoryEntry(
                from_state=self._state,
                trigger=event,
                to_state=chosen.to_state,
                timestamp=time.time(),
            )
            self._state = chosen.to_state
            self._history.append(entry)
            return self._state

    def reset(self) -> None:
        """Return to the initial state and clear history."""
        with self._lock:
            self._state = self._initial_state
            self._history.clear()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def can_trigger(self, event: str) -> bool:
        """Return ``True`` if *event* has at least one candidate from the
        current state (guards are **not** evaluated here).
        """
        with self._lock:
            return bool(self._index.get((self._state, event)))

    def available_triggers(self) -> list[str]:
        """Return de-duplicated list of trigger names valid from current state."""
        with self._lock:
            seen: set[str] = set()
            result: list[str] = []
            for from_state, trigger in self._index:
                if from_state == self._state and trigger not in seen:
                    seen.add(trigger)
                    result.append(trigger)
            return result

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"StateMachine(name={self._name!r}, state={self._state!r})"


# ---------------------------------------------------------------------------
# StateMachineDefinition
# ---------------------------------------------------------------------------


class StateMachineDefinition:
    """Validated blueprint for creating :class:`StateMachine` instances.

    Args:
        name:        Name propagated to every created machine.
        states:      Exhaustive list of valid state names.
        initial:     The starting state (must be in *states*).
        transitions: All edges; each state referenced must appear in *states*.

    Example::

        defn = StateMachineDefinition(
            name="traffic_light",
            states=["red", "green", "yellow"],
            initial="red",
            transitions=[
                Transition("red",    "green",  "go"),
                Transition("green",  "yellow", "slow"),
                Transition("yellow", "red",    "stop"),
            ],
        )
        light = defn.create()
        light.trigger("go")
    """

    def __init__(
        self,
        name: str,
        states: list[str],
        initial: str,
        transitions: list[Transition],
    ) -> None:
        self._name = name
        self._states = list(states)
        self._initial = initial
        self._transitions = list(transitions)

    def validate(self) -> None:
        """Check structural consistency.

        Raises:
            ValueError: When *initial* is not in *states*, or when any
                        transition references an unknown state.
        """
        state_set = set(self._states)
        if self._initial not in state_set:
            raise ValueError(
                f"[{self._name}] initial state {self._initial!r} "
                f"is not in states list {sorted(state_set)}"
            )
        for t in self._transitions:
            for attr, val in (("from_state", t.from_state), ("to_state", t.to_state)):
                if val not in state_set:
                    raise ValueError(
                        f"[{self._name}] transition {t.trigger!r}: "
                        f"{attr} {val!r} is not in states list"
                    )

    def create(self) -> StateMachine:
        """Validate the definition and return a new :class:`StateMachine`."""
        self.validate()
        return StateMachine(
            name=self._name,
            initial_state=self._initial,
            transitions=list(self._transitions),
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def states(self) -> list[str]:
        return list(self._states)

    @property
    def initial(self) -> str:
        return self._initial

    @property
    def transitions(self) -> list[Transition]:
        return list(self._transitions)


# ---------------------------------------------------------------------------
# Pre-built definitions
# ---------------------------------------------------------------------------

#: Runner lifecycle — mirrors the control plane states in ``control.py``.
#:
#: ASCII state diagram::
#:
#:   [start]
#:      │
#:      ▼
#:   ┌──────┐  start   ┌──────────┐  started  ┌─────────┐
#:   │ idle │ ───────► │ starting │ ─────────► │ running │◄──┐
#:   └──────┘          └──────────┘            └─────────┘   │
#:      ▲                                       │  │  │       │ resume
#:      │ reset                         pause   │  │  │fail   │
#:      │                               ▼       │  │  ▼       │
#:      │                         ┌────────┐    │  │ ┌────────┐
#:      │                         │pausing │    │  │ │ failed │
#:      │                         └────────┘    │  │ └────────┘
#:      │                               │paused │  │
#:      │                               ▼       │  │ stop
#:      │                         ┌────────┐    │  ▼
#:      │                         │ paused │    │ ┌──────────┐  stopped  ┌─────────┐
#:      │                         └────────┘    └►│ stopping │ ─────────►│ stopped │
#:      │                                         └──────────┘           └─────────┘
#:      │                                                                      │
#:      └──────────────────────────────────────────────────────────────────────┘
#:                                   (reset)
RUNNER_LIFECYCLE: StateMachineDefinition = StateMachineDefinition(
    name="runner",
    states=["idle", "starting", "running", "pausing", "paused", "stopping", "stopped", "failed"],
    initial="idle",
    transitions=[
        # Normal start path
        Transition("idle", "starting", "start"),
        Transition("starting", "running", "started"),
        # Pause / resume
        Transition("running", "pausing", "pause"),
        Transition("pausing", "paused", "paused"),
        Transition("paused", "running", "resume"),
        # Stop from any active state
        Transition("running", "stopping", "stop"),
        Transition("pausing", "stopping", "stop"),
        Transition("paused", "stopping", "stop"),
        Transition("starting", "stopping", "stop"),
        Transition("stopping", "stopped", "stopped"),
        # Failure paths
        Transition("starting", "failed", "fail"),
        Transition("running", "failed", "fail"),
        Transition("pausing", "failed", "fail"),
        Transition("paused", "failed", "fail"),
        Transition("stopping", "failed", "fail"),
        # Reset from terminal states
        Transition("stopped", "idle", "reset"),
        Transition("failed", "idle", "reset"),
    ],
)

#: Schedule run lifecycle — mirrors the ``ScheduleRunState`` vocabulary in
#: ADR-0062.
#:
#: ASCII state diagram::
#:
#:   [trigger]
#:      │
#:      ▼
#:   ┌─────────┐  activate  ┌────────┐  run   ┌─────────┐
#:   │ pending │ ──────────►│ active │────────►│ running │
#:   └─────────┘            └────────┘         └─────────┘
#:                           │  │  │            │  │  │  │
#:                  cancel   │  │  │   complete  │  │  │  │ fail
#:                           │  │  │            ▼  │  │  ▼
#:                           │  │  │     ┌───────────┐ ┌────────┐
#:                           │  │  │     │ completed │ │ failed │
#:                           │  │  │     └───────────┘ └────────┘
#:                           │  │  │
#:                    pause  │  │  │ cancel
#:                           ▼  │  ▼
#:                     ┌────────┐ ┌───────────┐
#:                     │ paused │ │ cancelled │
#:                     └────────┘ └───────────┘
#:                          │
#:                          │ resume
#:                          ▼
#:                       (active)
SCHEDULE_LIFECYCLE: StateMachineDefinition = StateMachineDefinition(
    name="schedule_run",
    states=["pending", "active", "running", "paused", "completed", "failed", "cancelled"],
    initial="pending",
    transitions=[
        # Activation
        Transition("pending", "active", "activate"),
        # Run
        Transition("active", "running", "run"),
        # Completion
        Transition("running", "completed", "complete"),
        # Failure
        Transition("running", "failed", "fail"),
        Transition("active", "failed", "fail"),
        # Pause / resume
        Transition("active", "paused", "pause"),
        Transition("running", "paused", "pause"),
        Transition("paused", "active", "resume"),
        # Cancel
        Transition("pending", "cancelled", "cancel"),
        Transition("active", "cancelled", "cancel"),
        Transition("running", "cancelled", "cancel"),
        Transition("paused", "cancelled", "cancel"),
    ],
)


__all__ = [
    "HistoryEntry",
    "RUNNER_LIFECYCLE",
    "SCHEDULE_LIFECYCLE",
    "State",
    "StateMachine",
    "StateMachineDefinition",
    "StateMachineError",
    "Transition",
]
