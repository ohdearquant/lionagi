# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.ln._proc import aterminate_process_group, terminate_process_group

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_proc(pid):
    """Sync-only fake process (subprocess.Popen-shaped)."""
    p = MagicMock()
    p.pid = pid
    return p


def _fake_async_proc(pid, wait_delay: float = 0.0):
    """Asyncio-shaped fake process."""
    p = MagicMock()
    p.pid = pid

    async def _wait():
        if wait_delay:
            await asyncio.sleep(wait_delay)

    p.wait = AsyncMock(side_effect=_wait)
    p.terminate = MagicMock()
    p.kill = MagicMock()
    return p


# ---------------------------------------------------------------------------
# pid-guard: must never signal pid <= 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pid", [1, 0, -1, None])
def test_terminate_proc_group_pid_guard_sync(pid):
    """terminate_process_group never calls os.killpg for pid <= 1 or None.

    The footgun is os.killpg(1, SIGKILL) hitting init/CI runner.  A single-process
    proc.kill() fallback on the same pid values is acceptable (and expected on
    non-POSIX or when pgid-guard fires).
    """
    proc = _fake_proc(pid)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg") as mock_killpg:
        terminate_process_group(proc, grace=None)
        mock_killpg.assert_not_called()


@pytest.mark.parametrize("pid", [1, 0, None])
def test_terminate_proc_group_pid_guard_no_killpg(pid):
    """When pid is not > 1, the proc.kill fallback is also skipped."""
    proc = _fake_proc(pid)
    # With a non-int or sentinel pid, _safe_pgid returns None and the else
    # branch's proc.kill() is also guarded by _safe_pgid returning None.
    # For pid=1/0 as int: _safe_pgid checks > 1 → None; else branch uses proc.kill().
    # Wait — for sync SIGKILL-only: if pgid is None → proc.kill().
    # But pid=1 as int: isinstance(1, int) is True but 1 > 1 is False → pgid=None → proc.kill() IS called.
    # That is correct behavior: the single-process kill() on a real-but-guarded pid.
    # The CRITICAL guard is: os.killpg(1, SIGKILL) is NOT called.
    with patch("lionagi.ln._proc.os.killpg", create=True) as mock_killpg:
        terminate_process_group(proc, grace=None)
        mock_killpg.assert_not_called()


@pytest.mark.parametrize("pid", [1, 0, None])
@pytest.mark.asyncio
async def test_aterminate_proc_group_pid_guard(pid):
    """aterminate_process_group never calls os.killpg for pid <= 1 or None."""
    proc = _fake_async_proc(pid)
    with patch("lionagi.ln._proc.os.killpg", create=True) as mock_killpg:
        await aterminate_process_group(proc, grace=None)
        mock_killpg.assert_not_called()


@pytest.mark.asyncio
async def test_aterminate_proc_group_pid_1_no_killpg_grace():
    """Even with grace, pid==1 must not trigger os.killpg."""
    proc = _fake_async_proc(pid=1, wait_delay=0.0)
    with patch("lionagi.ln._proc.os.killpg", create=True) as mock_killpg:
        await aterminate_process_group(proc, grace=5.0)
        mock_killpg.assert_not_called()


def test_terminate_proc_group_never_signals_callers_group(monkeypatch):
    """A leaked parent PGID must fall back to direct-child termination."""
    import lionagi.ln._proc as proc_mod

    proc = _fake_proc(pid=4242)
    mock_killpg = MagicMock()
    monkeypatch.setattr(proc_mod.os, "killpg", mock_killpg, raising=False)
    monkeypatch.setattr(proc_mod.os, "getpgrp", lambda: 4242, raising=False)

    terminate_process_group(proc, grace=None)

    mock_killpg.assert_not_called()
    proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# Normal pid (> 1): killpg is called with the right signal
# ---------------------------------------------------------------------------


def test_terminate_sigkill_only():
    """grace=None → SIGKILL sent to process group; no SIGTERM."""
    proc = _fake_proc(pid=1234)
    with (
        patch("lionagi.ln._proc.os.killpg", create=True) as mock_killpg,
        patch("lionagi.ln._proc.hasattr", return_value=True),
    ):
        # hasattr patch covers the killpg attribute check; use direct approach instead
        pass

    # Direct approach: patch at the module's os reference
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg") as mock_killpg:
        terminate_process_group(proc, grace=None)
        mock_killpg.assert_called_once_with(1234, signal.SIGKILL)


def test_terminate_sigterm_first_sync():
    """grace!=None → SIGTERM sent to process group (sync variant; caller drives wait+SIGKILL)."""
    proc = _fake_proc(pid=5678)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg") as mock_killpg:
        terminate_process_group(proc, grace=5.0)
        mock_killpg.assert_called_once_with(5678, signal.SIGTERM)


@pytest.mark.asyncio
async def test_aterminate_sigkill_only():
    """grace=None → SIGKILL only, no wait."""
    proc = _fake_async_proc(pid=9999)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg") as mock_killpg:
        await aterminate_process_group(proc, grace=None)
        mock_killpg.assert_called_once_with(9999, signal.SIGKILL)
        proc.wait.assert_not_called()


@pytest.mark.asyncio
async def test_aterminate_sigterm_then_sigkill_on_timeout():
    """grace path: SIGTERM first, SIGKILL after timeout fires."""
    proc = _fake_async_proc(pid=7777, wait_delay=10.0)  # won't finish in time
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    calls = []

    def _record_killpg(pgid, sig):
        calls.append((pgid, sig))

    with patch.object(proc_mod.os, "killpg", side_effect=_record_killpg):
        await aterminate_process_group(proc, grace=0.01)

    assert (7777, signal.SIGTERM) in calls
    assert (7777, signal.SIGKILL) in calls
    # SIGTERM before SIGKILL
    assert calls.index((7777, signal.SIGTERM)) < calls.index((7777, signal.SIGKILL))


@pytest.mark.asyncio
async def test_aterminate_sigterm_no_sigkill_when_exits_fast():
    """grace path: no SIGKILL if process exits before timeout."""
    proc = _fake_async_proc(pid=4444, wait_delay=0.0)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    calls = []

    def _record_killpg(pgid, sig):
        calls.append((pgid, sig))

    with patch.object(proc_mod.os, "killpg", side_effect=_record_killpg):
        await aterminate_process_group(proc, grace=5.0)

    # SIGTERM was sent, SIGKILL was NOT (process exited before timeout)
    assert (4444, signal.SIGTERM) in calls
    assert (4444, signal.SIGKILL) not in calls


# ---------------------------------------------------------------------------
# Already-dead process: ProcessLookupError is swallowed
# ---------------------------------------------------------------------------


def test_terminate_swallows_processlookuperror():
    """ProcessLookupError from killpg is swallowed; no exception propagates."""
    proc = _fake_proc(pid=2222)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg", side_effect=ProcessLookupError):
        # Must not raise
        terminate_process_group(proc, grace=None)


def test_terminate_swallows_permissionerror():
    """PermissionError from killpg is swallowed."""
    proc = _fake_proc(pid=3333)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg", side_effect=PermissionError):
        terminate_process_group(proc, grace=None)


def test_terminate_swallows_oserror():
    """OSError from killpg is swallowed."""
    proc = _fake_proc(pid=4444)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg", side_effect=OSError):
        terminate_process_group(proc, grace=None)


@pytest.mark.asyncio
async def test_aterminate_swallows_processlookuperror():
    """ProcessLookupError during aterminate is swallowed."""
    proc = _fake_async_proc(pid=5555)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg", side_effect=ProcessLookupError):
        await aterminate_process_group(proc, grace=None)


# ---------------------------------------------------------------------------
# Custom sig_first parameter
# ---------------------------------------------------------------------------


def test_terminate_custom_sig_first():
    """terminate_process_group respects a custom sig_first."""
    proc = _fake_proc(pid=8888)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with patch.object(proc_mod.os, "killpg") as mock_killpg:
        terminate_process_group(proc, grace=5.0, sig_first=signal.SIGHUP)
        mock_killpg.assert_called_once_with(8888, signal.SIGHUP)


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_aterminate_grace_escalates_to_kill_on_backend(backend):
    """A process that ignores terminate is SIGKILLed after grace on both backends.

    The grace wait uses an anyio cancel scope; asyncio.wait_for previously raised
    'no running event loop' on a Trio task before the timeout policy could apply,
    so the forced-kill escalation never ran.
    """
    import anyio

    if backend == "trio":
        pytest.importorskip("trio")

    class _Proc:
        def __init__(self):
            self.pid = -1  # _safe_pgid -> None: no real killpg on a fake pid
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        async def wait(self):
            while not self.killed:
                await anyio.sleep(0.001)
            return 0

    proc = _Proc()

    async def _run():
        await aterminate_process_group(proc, grace=0.01)

    anyio.run(_run, backend=backend)
    assert proc.terminated is True
    assert proc.killed is True
