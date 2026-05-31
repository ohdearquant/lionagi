# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class RunnerState(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RunnerState.COMPLETED,
            RunnerState.FAILED,
            RunnerState.TIMED_OUT,
            RunnerState.KILLED,
        }


class ControlVerb(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    KILL = "kill"
    RETRY = "retry"


VALID_TRANSITIONS: dict[RunnerState, set[RunnerState]] = {
    RunnerState.PENDING: {
        RunnerState.PREPARING,
        RunnerState.FAILED,
        RunnerState.KILLED,
        RunnerState.CANCELLING,
    },
    RunnerState.PREPARING: {
        RunnerState.RUNNING,
        RunnerState.FAILED,
        RunnerState.TIMED_OUT,
        RunnerState.KILLED,
        RunnerState.CANCELLING,
    },
    RunnerState.RUNNING: {
        RunnerState.PAUSED,
        RunnerState.COMPLETED,
        RunnerState.FAILED,
        RunnerState.TIMED_OUT,
        RunnerState.CANCELLING,
        RunnerState.KILLED,
    },
    RunnerState.PAUSED: {
        RunnerState.RUNNING,
        RunnerState.CANCELLING,
        RunnerState.KILLED,
    },
    RunnerState.CANCELLING: {
        RunnerState.FAILED,
        RunnerState.KILLED,
        RunnerState.TIMED_OUT,
    },
    RunnerState.COMPLETED: set(),
    RunnerState.FAILED: set(),
    RunnerState.KILLED: set(),
    RunnerState.TIMED_OUT: set(),
}


def validate_transition(current: RunnerState | None, target: RunnerState) -> bool:
    if current is None:
        return False
    return target in VALID_TRANSITIONS[current]


class ControlRequest(BaseModel):
    verb: ControlVerb
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunnerHandle(BaseModel):
    session_id: str
    state: RunnerState
    runner_type: str
    pid: int | None = None
    started_at: datetime
    metadata: dict = Field(default_factory=dict)


__all__ = [
    "ControlRequest",
    "ControlVerb",
    "RunnerHandle",
    "RunnerState",
    "VALID_TRANSITIONS",
    "validate_transition",
]
