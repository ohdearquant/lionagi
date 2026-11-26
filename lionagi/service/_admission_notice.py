# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The notice a CLI agent spawn gives while it waits for a machine-wide slot.

The admission itself lives in ``lionhub.providers._subagent_admission``, beside the spawn
that starts CLI agent children. A run that times a worker's first output listens here, so
the core imports nothing from the hub to listen: with the hub not installed nothing starts a
CLI agent, no notice is given, and the run times its first output as it would anyway.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextvars import ContextVar

__all__ = ()


# Told when an agent started in this context begins waiting for slots and when it is
# admitted; see _on_subagent_admission.
_ON_ADMISSION: ContextVar[Callable[[bool], None] | None] = ContextVar(
    "lionagi_subagent_on_admission", default=None
)


@contextlib.contextmanager
def _on_subagent_admission(callback: Callable[[bool], None]) -> Iterator[None]:
    """Call ``callback(False)`` when a CLI agent started in this context starts waiting for
    its slots, and ``callback(True)`` once it is admitted.

    A caller timing the agent's first output stops its clock for the wait and starts it
    at admission, so the time spent waiting for a slot is not read as a worker that
    never spoke.
    """
    token = _ON_ADMISSION.set(callback)
    try:
        yield
    finally:
        _ON_ADMISSION.reset(token)


def _tell_admission(admitted: bool) -> None:
    callback = _ON_ADMISSION.get()
    if callback is not None:
        callback(admitted)
