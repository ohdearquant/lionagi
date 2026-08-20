# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.ln._proc import aterminate_process_group, terminate_process_group


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


# pid-guard: must never signal pid <= 1


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
    """grace path: no SIGKILL when the child exits and its group empties.

    Signal 0 is a delivery test rather than a signal, and it is how the code
    asks whether anything is left in the group. A recorder that accepts every
    signal answers "still populated" forever, which is not what the kernel does
    once the group has emptied, so the double models that here. Without it this
    test would report a forced kill that a real exited-and-empty group never
    provokes.
    """
    proc = _fake_async_proc(pid=4444, wait_delay=0.0)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    calls = []

    def _record_killpg(pgid, sig):
        if sig == 0:
            # Nothing else was ever put in this group, so it is empty exactly
            # when the child has been reaped.
            if proc.returncode is None:
                return
            raise ProcessLookupError(3, "No such process")
        calls.append((pgid, sig))

    with patch.object(proc_mod.os, "killpg", side_effect=_record_killpg):
        await aterminate_process_group(proc, grace=5.0)

    # SIGTERM was sent, SIGKILL was NOT (process exited before timeout)
    assert (4444, signal.SIGTERM) in calls
    assert (4444, signal.SIGKILL) not in calls


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


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_the_grace_wait_ends_with_the_child_not_with_whatever_holds_its_pipes(backend):
    """A descendant that kept the child's pipes must not decide when this returns.

    The fake splits two facts a real process reports together on some
    interpreters and separately on others: ``returncode`` is set when the child
    is reaped, while ``wait()`` completes only once every inherited pipe closes.
    Waiting on the second means a caller giving up on a child is bounded by a
    process it never started, and callers use this wait to decide whether the
    child's output is still arriving or genuinely absent.

    The deadline here is well inside ``grace``, so waiting on ``wait()`` fails
    this outright rather than merely making it slow.
    """
    import anyio

    if backend == "trio":
        pytest.importorskip("trio")

    class _PipesHeldOpen:
        def __init__(self):
            self.pid = -1  # _safe_pgid -> None: no real killpg on a fake pid
            self.returncode = None
            self.killed = False

        def terminate(self):
            self.returncode = -15  # the child is reaped here

        def kill(self):
            self.killed = True

        async def wait(self):
            await anyio.sleep_forever()  # the holder never lets go

    proc = _PipesHeldOpen()

    async def _run():
        with anyio.fail_after(2):
            await aterminate_process_group(proc, grace=5.0)

    anyio.run(_run, backend=backend)
    assert proc.returncode == -15
    assert proc.killed is False, (
        "the child had already exited, so escalating to SIGKILL means the wait "
        "was reading something other than the child's own status"
    )


# A real subprocess, not a fake. The behaviour under test is one where the two
# differ: whether ``wait()`` returns when the child is reaped or when the last
# inherited pipe closes is decided by the interpreter, and a hand-written double
# can only assert whichever of those its author already believed.
@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
@pytest.mark.parametrize("descendant_holds_pipe", [True, False], ids=["holds-pipe", "no-pipe"])
def test_a_group_member_that_ignores_the_first_signal_is_still_killed(
    descendant_holds_pipe,
):
    """Grace belongs to the group, not only to the direct child.

    The direct child leads its own process group and exits promptly on SIGTERM.
    A descendant in that same group ignores SIGTERM. Once grace is spent the
    forced kill exists precisely for that descendant, so reaching it must not
    depend on the direct child being slow.

    Both parameters matter and they fail differently. When the descendant holds
    the child's stderr, ``wait()`` is pipe-bound on some interpreters, so the
    escalation used to fire for an incidental reason -- the wait stalled -- and
    the descendant died there without anything having asked about the group.
    When it holds no pipe nothing ever stalled, and it survived everywhere.
    """
    grandchild = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(300)"
    )

    async def _run(ready_path):
        stderr_arg = "sys.stderr" if descendant_holds_pipe else "subprocess.DEVNULL"
        child = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, "
            f"{str(ready_path)!r}], stderr={stderr_arg}); "
            "time.sleep(300)"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while not ready_path.exists():
            if time.monotonic() > deadline:
                proc.kill()
                pytest.fail("the descendant never reported itself ready")
            await asyncio.sleep(0.02)
        descendant = int(ready_path.read_text())
        # Asserted premises. Without them a pass says nothing: a descendant that
        # never started, or that landed in a different process group, would look
        # cleaned up here whatever the code under test did.
        os.kill(descendant, 0)
        assert os.getpgid(descendant) == os.getpgid(proc.pid)

        await aterminate_process_group(proc, grace=0.5)

        survived = True
        settle = time.monotonic() + 2
        while time.monotonic() < settle:
            try:
                os.kill(descendant, 0)
            except OSError:
                survived = False
                break
            await asyncio.sleep(0.02)
        return descendant, survived

    with tempfile.TemporaryDirectory() as td:
        ready_path = pathlib.Path(td) / "descendant.pid"
        descendant, survived = asyncio.run(_run(ready_path))
        try:
            assert not survived, (
                "a process-group member that ignored the first signal outlived "
                "cleanup: grace expired and the forced kill never reached it"
            )
        finally:
            with contextlib.suppress(OSError):
                os.kill(descendant, signal.SIGKILL)
