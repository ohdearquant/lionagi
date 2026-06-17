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
    # Guard: pid must be a real int greater than 1.
    # pid==0 targets the caller's own group; pid==1 is init/the session leader
    # on CI runners, which would SIGKILL the test harness itself.  A MagicMock
    # whose .pid coerces to 1 via __index__ is therefore a silent no-op here.
    # os.killpg is POSIX-only; on Windows leave None so callers fall back to
    # proc.terminate()/kill() rather than raising AttributeError.
    if not (hasattr(os, "killpg") and isinstance(pid, int) and pid > 1):
        return None
    return pid


def terminate_process_group(
    proc: Any,
    *,
    grace: float | None = None,
    sig_first: signal.Signals = signal.SIGTERM,
) -> None:
    """Send sig_first to the process group then SIGKILL after grace seconds.

    If grace is None, send SIGKILL immediately with no prior SIGTERM.  The
    sync variant only sends the first signal; callers are responsible for
    waiting and escalating to SIGKILL (use aterminate_process_group for the
    full async SIGTERM-wait-SIGKILL cycle).  Swallows ProcessLookupError,
    PermissionError, and OSError so an already-dead process never raises.
    Does nothing when proc.pid is None, 0, or <=1 (pid-guard).
    """
    pgid = _safe_pgid(proc)
    if grace is None:
        # SIGKILL-only path
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        return
    # SIGTERM (or sig_first) only — the caller drives the wait + SIGKILL escalation
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig_first)
    else:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()


async def aterminate_process_group(
    proc: Any,
    *,
    grace: float | None = None,
    sig_first: signal.Signals = signal.SIGTERM,
) -> None:
    """Async: send sig_first to the process group, wait up to grace, then SIGKILL.

    If grace is None, send SIGKILL immediately with no prior signal.  Swallows
    ProcessLookupError, PermissionError, and OSError.  Does nothing when
    proc.pid is None, 0, or <=1 (pid-guard).
    """
    pgid = _safe_pgid(proc)
    if grace is None:
        # SIGKILL-only path (no SIGTERM, no wait)
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        return
    # SIGTERM-then-wait-then-SIGKILL path
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig_first)
    else:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except (asyncio.TimeoutError, TimeoutError):
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
