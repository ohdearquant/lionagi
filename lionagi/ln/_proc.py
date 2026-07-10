# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import Any


def _safe_pgid(proc: Any) -> int | None:
    """Return the process-group id to signal, or None when unsafe."""
    pid = getattr(proc, "pid", None)
    # pid must be int > 1: pid==0 is our own group, pid==1 is init/session leader
    # on CI (would SIGKILL the harness itself; also catches MagicMock.pid==1).
    # killpg is POSIX-only; None here makes callers fall back to proc.terminate()/kill().
    if not (hasattr(os, "killpg") and isinstance(pid, int) and pid > 1):
        return None
    return pid


def terminate_process_group(
    proc: Any,
    *,
    grace: float | None = None,
    sig_first: signal.Signals = signal.SIGTERM,
) -> None:
    """Send sig_first to the process group AND the direct child.

    If grace is None, send SIGKILL immediately with no prior SIGTERM.  The
    sync variant only sends the first signal; callers are responsible for
    waiting and escalating to SIGKILL (use aterminate_process_group for the
    full async SIGTERM-wait-SIGKILL cycle).  Swallows ProcessLookupError,
    PermissionError, and OSError so an already-dead process never raises.
    The pid-guard suppresses os.killpg for proc.pid None/0/<=1; the direct
    child is still signalled via proc.terminate()/kill().
    """
    pgid = _safe_pgid(proc)
    if grace is None:
        # Signal group AND direct child: proc.kill() is normally a no-op (child is
        # in the killed group) but prevents orphaning it when killpg is unavailable.
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return
    # sig_first only; caller drives the wait + SIGKILL escalation.
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig_first)
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()


async def aterminate_process_group(
    proc: Any,
    *,
    grace: float | None = None,
    sig_first: signal.Signals = signal.SIGTERM,
) -> None:
    """Async: signal the process group AND the direct child, wait up to grace, then SIGKILL.

    If grace is None, send SIGKILL immediately with no prior signal.  Swallows
    ProcessLookupError, PermissionError, and OSError.  The pid-guard suppresses
    os.killpg for proc.pid None/0/<=1; the direct child is still signalled via
    proc.terminate()/kill().
    """
    pgid = _safe_pgid(proc)
    if grace is None:
        # No prior SIGTERM/wait: signal group AND direct child directly.
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig_first)
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except (asyncio.TimeoutError, TimeoutError):
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
