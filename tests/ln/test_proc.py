# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import pathlib
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from lionagi.ln._proc import (
    aterminate_process_group,
    capture_group_witnesses,
    observe_process_group_leader,
    prepare_process_group_env,
    process_create_time,
    raw_create_time,
    terminate_process_group,
)


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


def test_raw_create_time_ignores_boot_time_clock_slew(monkeypatch):
    """raw_create_time is immune to the boot-time-drift adjustment; the plain
    psutil call is not.

    psutil >= 7.1 adjusts ``Process.create_time()`` on the platforms that
    support it (macOS, NetBSD) by ``INIT_BOOT_TIME - boot_time()``, where
    ``INIT_BOOT_TIME`` is frozen at psutil's own import time
    (``psutil._psosx.adjust_proc_create_time``). ``kern.boottime`` moves under
    NTP clock slew, so that offset drifts open while a process runs — a
    recorded value and a freshly-read one for the SAME live process stop
    matching. Reproduce the mechanism directly by moving the frozen value,
    rather than waiting on a real slew: a fresh ``Process`` handle should then
    read a plain ``create_time()`` that has moved by the same amount, while
    ``raw_create_time`` on the same handle has not moved at all.
    """
    import psutil

    psplatform = psutil._psplatform
    if not hasattr(psplatform, "INIT_BOOT_TIME"):
        pytest.skip("this platform's psutil backend has no boot-time adjustment")

    pid = os.getpid()
    baseline_adjusted = psutil.Process(pid).create_time()
    baseline_raw = raw_create_time(psutil.Process(pid))

    # Shift the frozen value itself rather than rebuilding it from
    # psutil.boot_time(): the shift is then exactly one second whatever the
    # sysctl reports, and the test does not fail on a platform that denies
    # that read.
    monkeypatch.setattr(psplatform, "INIT_BOOT_TIME", psplatform.INIT_BOOT_TIME + 1.0)

    # A fresh handle: psutil.Process caches create_time() after first call, so
    # reusing the process above would hide the drift behind that cache rather
    # than exercising the adjustment.
    drifted = psutil.Process(pid)
    adjusted = drifted.create_time()
    raw = raw_create_time(drifted)

    assert adjusted == pytest.approx(baseline_adjusted + 1.0, abs=0.01)
    assert raw == pytest.approx(baseline_raw, abs=0.01)


class _RecordingProc:
    """A psutil-shaped stub that records how its start time was read."""

    def __init__(self):
        self.calls: list[dict] = []
        self._proc = self

    def create_time(self, **kwargs):
        self.calls.append(kwargs)
        return 1234.5


def test_raw_create_time_reader_skips_monotonic_without_the_boot_adjustment(monkeypatch):
    """A backend that adjusts nothing and is not Linux keeps the plain epoch reading.

    ``monotonic=True`` does not mean the same thing on every psutil backend.
    Where nothing is adjusted the keyword can change what the value counts
    from (on Linux it omits the ``boot_time()`` addition and yields seconds
    since boot), so taking it bare would redefine every recorded
    ``pid_create_time``, make live processes stop matching their own records,
    and let a post-reboot pid reuse at a similar uptime match a stranger.
    Exercised through the selector's seam because the branch belongs to a
    platform this suite may not run on.
    """
    import psutil

    from lionagi.ln import _proc

    class _UnadjustedProcess:
        # The keyword is accepted and nothing is adjusted, so offering it is
        # exactly what makes the wrong reader available.
        def create_time(self, monotonic=False): ...

    monkeypatch.setattr(psutil, "LINUX", False)
    monkeypatch.setattr(psutil, "_psplatform", SimpleNamespace(Process=_UnadjustedProcess))

    reader = _proc._select_raw_create_time_reader()
    proc = _RecordingProc()

    assert reader(proc) == 1234.5
    assert proc.calls == [{}]


def test_raw_create_time_reader_takes_monotonic_where_the_adjustment_exists(monkeypatch):
    """The backend that applies the slewing adjustment is the one to bypass."""
    import psutil

    from lionagi.ln import _proc

    class _AdjustingProcess:
        def create_time(self, monotonic=False): ...

    monkeypatch.setattr(psutil, "LINUX", False)
    monkeypatch.setattr(
        psutil,
        "_psplatform",
        SimpleNamespace(
            adjust_proc_create_time=lambda value: value,
            Process=_AdjustingProcess,
        ),
    )

    reader = _proc._select_raw_create_time_reader()
    proc = _RecordingProc()

    assert reader(proc) == 1234.5
    assert proc.calls == [{"monotonic": True}]


class _LinuxBackendProcess:
    """psutil's Linux backend: a fixed start counted from boot, plus the boot
    time read again on every epoch call."""

    def __init__(self, since_boot=0.0, boot_time=None):
        self._since_boot = since_boot
        self._boot_time = boot_time

    def create_time(self, monotonic=False):
        if monotonic:
            return self._since_boot
        return self._since_boot + self._boot_time()


class _LinuxShapedProc:
    """A psutil handle shaped like Linux's, its backend the platform process."""

    def __init__(self, since_boot, boot_time):
        self._proc = _LinuxBackendProcess(since_boot, boot_time)

    def create_time(self):
        return self._proc.create_time()


def _stepping_boot_clock():
    """The kernel's whole-second boot time: 1000 on the first read, 1001 after."""
    reads = []

    def boot_time():
        reads.append(True)
        return 1000.0 if len(reads) == 1 else 1001.0

    return boot_time, reads


def test_raw_create_time_on_linux_does_not_move_with_the_boot_time(monkeypatch):
    """Two reads of one process are equal on Linux even when ``btime`` steps.

    psutil's Linux backend adds ``boot_time()`` to a fixed start counted from
    boot, and reads ``btime`` from ``/proc/stat`` again on every call. A clock
    step or slew moves that whole-second value, so the plain call for one live
    process can read a second later than its spawn record, which is outside the
    tolerance and makes a teardown refuse to signal the group.
    """
    import psutil

    from lionagi.ln import _proc

    boot_time, reads = _stepping_boot_clock()
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(
        psutil,
        "_psplatform",
        SimpleNamespace(Process=_LinuxBackendProcess, boot_time=boot_time),
    )

    reader = _proc._select_raw_create_time_reader()
    proc = _LinuxShapedProc(5.0, boot_time)

    first = reader(proc)
    second = reader(proc)

    assert first == second == 1005.0
    assert len(reads) == 1


def test_raw_create_time_on_linux_reads_a_stand_in_through_the_public_call(monkeypatch):
    """Only psutil's own Linux backend counts from boot.

    A handle whose backend is anything else (a test double, an adapter) is read
    through its public call, so a stand-in scripted with an epoch start time
    answers that time rather than that time plus the boot time.
    """
    import psutil

    from lionagi.ln import _proc

    boot_time, _ = _stepping_boot_clock()
    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(
        psutil,
        "_psplatform",
        SimpleNamespace(Process=_LinuxBackendProcess, boot_time=boot_time),
    )

    reader = _proc._select_raw_create_time_reader()
    proc = _RecordingProc()

    assert reader(proc) == 1234.5
    assert proc.calls == [{}]


def test_raw_create_time_on_linux_keeps_the_plain_call_without_a_boot_time(monkeypatch):
    """A Linux backend whose boot time cannot be read keeps the public call."""
    import psutil

    from lionagi.ln import _proc

    def boot_time():
        raise OSError("procfs unreadable")

    monkeypatch.setattr(psutil, "LINUX", True)
    monkeypatch.setattr(
        psutil,
        "_psplatform",
        SimpleNamespace(Process=_LinuxBackendProcess, boot_time=boot_time),
    )

    reader = _proc._select_raw_create_time_reader()
    proc = _RecordingProc()

    assert reader(proc) == 1234.5
    assert proc.calls == [{}]


def test_raw_create_time_selects_its_reader_once_on_first_read(monkeypatch):
    """Selection is deferred to the first read, then cached.

    Importing this module must not cost a psutil import and two signature
    inspections on the many paths that never read a start time.
    """
    from lionagi.ln import _proc

    selections = []

    def _select():
        selections.append(True)
        return lambda proc: 7.0

    monkeypatch.setattr(_proc, "_read_raw_create_time", None)
    monkeypatch.setattr(_proc, "_select_raw_create_time_reader", _select)

    assert _proc.raw_create_time(object()) == 7.0
    assert _proc.raw_create_time(object()) == 7.0
    assert len(selections) == 1


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


def test_immediate_sync_cleanup_without_posix_signals_is_direct_child_only(monkeypatch):
    """Windows-like platforms need no SIGKILL attribute for child cleanup."""
    import lionagi.ln._proc as proc_mod

    proc = _fake_proc(pid=4242)
    monkeypatch.delattr(proc_mod.os, "killpg", raising=False)
    monkeypatch.setattr(proc_mod, "signal", SimpleNamespace())

    terminate_process_group(proc, grace=None)

    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_immediate_async_cleanup_without_posix_signals_is_direct_child_only(
    monkeypatch,
):
    """The async direct-child fallback is portable when SIGKILL is absent."""
    import lionagi.ln._proc as proc_mod

    proc = _fake_async_proc(pid=4242)
    monkeypatch.delattr(proc_mod.os, "killpg", raising=False)
    monkeypatch.setattr(proc_mod, "signal", SimpleNamespace())

    await aterminate_process_group(proc, grace=None)

    proc.kill.assert_called_once()


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

    with (
        patch.object(proc_mod, "_group_has_witness", return_value=True),
        patch.object(proc_mod.os, "killpg") as mock_killpg,
    ):
        terminate_process_group(proc, grace=None, group_witnesses=((1234, 10.0),))
        mock_killpg.assert_called_once_with(1234, signal.SIGKILL)


def test_terminate_sigterm_first_sync():
    """grace!=None → SIGTERM sent to process group (sync variant; caller drives wait+SIGKILL)."""
    proc = _fake_proc(pid=5678)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with (
        patch.object(proc_mod, "_group_has_witness", return_value=True),
        patch.object(proc_mod.os, "killpg") as mock_killpg,
    ):
        terminate_process_group(proc, grace=5.0, group_witnesses=((5678, 10.0),))
        mock_killpg.assert_called_once_with(5678, signal.SIGTERM)


@pytest.mark.asyncio
async def test_aterminate_sigkill_only():
    """grace=None → SIGKILL only, no wait."""
    proc = _fake_async_proc(pid=9999)
    import os as real_os

    import lionagi.ln._proc as proc_mod

    if not hasattr(real_os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    with (
        patch.object(proc_mod, "_group_has_witness", return_value=True),
        patch.object(proc_mod.os, "killpg") as mock_killpg,
    ):
        await aterminate_process_group(
            proc,
            grace=None,
            group_witnesses=((9999, 10.0),),
        )
        mock_killpg.assert_called_once_with(9999, signal.SIGKILL)
        proc.wait.assert_not_called()


def test_immediate_sync_cleanup_without_spawn_witness_is_child_only():
    """The immediate sync path does not invent group authority at teardown."""
    import lionagi.ln._proc as proc_mod

    proc = SimpleNamespace(pid=9999, returncode=0, kill=MagicMock())
    with patch.object(proc_mod.os, "killpg") as killpg:
        terminate_process_group(proc, grace=None)

    killpg.assert_not_called()
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_immediate_async_cleanup_without_spawn_witness_is_child_only():
    """The immediate async path does not invent group authority at teardown."""
    import lionagi.ln._proc as proc_mod

    proc = SimpleNamespace(pid=9999, returncode=0, kill=MagicMock())
    with patch.object(proc_mod.os, "killpg") as killpg:
        await aterminate_process_group(proc, grace=None)

    killpg.assert_not_called()
    proc.kill.assert_called_once()


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

    witnesses = ((7777, 10.0),)
    with (
        patch.object(proc_mod, "_group_has_witness", return_value=True),
        patch.object(proc_mod.os, "killpg", side_effect=_record_killpg),
    ):
        await aterminate_process_group(
            proc,
            grace=0.01,
            group_witnesses=witnesses,
        )

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

    with (
        patch.object(proc_mod, "_group_has_witness", return_value=True),
        patch.object(proc_mod.os, "killpg", side_effect=_record_killpg),
    ):
        await aterminate_process_group(
            proc,
            grace=5.0,
            group_witnesses=((4444, 10.0),),
        )

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

    with (
        patch.object(proc_mod, "_group_has_witness", return_value=True),
        patch.object(proc_mod.os, "killpg") as mock_killpg,
    ):
        terminate_process_group(
            proc,
            grace=5.0,
            sig_first=signal.SIGHUP,
            group_witnesses=((8888, 10.0),),
        )
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
_DESCENDANT = (
    "import os, pathlib, signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
    "time.sleep(300)"
)


def _terminate_with_a_descendant(*, escapes_group: bool, holds_pipe: bool, grace: float):
    """Run cleanup against a child that has a descendant, and report what happened.

    Returns ``(descendant_survived, elapsed, returncode)``. The descendant is
    killed on every exit path including setup failures, so a failed premise
    cannot leave a 300-second process behind on whatever machine ran the suite.
    """

    async def _still_alive(pid: int, settle: float) -> bool:
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            await asyncio.sleep(0.02)
        return True

    async def _run(ready_path):
        proc = None
        descendant = None
        try:
            stderr_arg = "sys.stderr" if holds_pipe else "subprocess.DEVNULL"
            own_session = ", start_new_session=True" if escapes_group else ""
            child = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {_DESCENDANT!r}, "
                f"{str(ready_path)!r}], stderr={stderr_arg}{own_session}); "
                "time.sleep(300)"
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                child,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            leader_state, leader_created = process_create_time(proc.pid)
            assert leader_state == "found" and leader_created is not None
            deadline = time.monotonic() + 20
            while not ready_path.exists():
                if time.monotonic() > deadline:
                    pytest.fail("the descendant never reported itself ready")
                await asyncio.sleep(0.02)
            descendant = int(ready_path.read_text())
            # Asserted premises. Without them a pass says nothing: a descendant
            # that never started, or that landed on the wrong side of the group
            # boundary, would look handled here whatever the code under test did.
            os.kill(descendant, 0)
            shares_group = os.getpgid(descendant) == os.getpgid(proc.pid)
            assert shares_group is not escapes_group, (
                f"descendant group placement is wrong for this case: "
                f"shares_group={shares_group}, escapes_group={escapes_group}"
            )
            witnesses = capture_group_witnesses(
                proc,
                expected_leader_created=leader_created,
            )
            assert witnesses, "the live child did not yield a spawn-bound group witness"

            started = time.monotonic()
            await aterminate_process_group(
                proc,
                grace=grace,
                group_witnesses=witnesses,
            )
            elapsed = time.monotonic() - started
            survived = await _still_alive(descendant, settle=2.0)
            return survived, elapsed, proc.returncode
        finally:
            if descendant is not None:
                with contextlib.suppress(OSError):
                    os.kill(descendant, signal.SIGKILL)
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()

    with tempfile.TemporaryDirectory() as td:
        return asyncio.run(_run(pathlib.Path(td) / "descendant.pid"))


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
    survived, _, _ = _terminate_with_a_descendant(
        escapes_group=False, holds_pipe=descendant_holds_pipe, grace=0.5
    )
    assert not survived, (
        "a process-group member that ignored the first signal outlived cleanup: "
        "grace expired and the forced kill never reached it"
    )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
def test_cleanup_is_not_bounded_by_a_descendant_that_escaped_with_the_pipes():
    """The other half of the same contract, and the one this pair exists to keep.

    Here the descendant leaves the child's process group and keeps the child's
    stderr. Nothing in this cleanup can signal it and nothing should wait for
    it: the child's own exit is the entire question being asked. Where ``wait()``
    is pipe-bound, waiting on it instead returns only once grace has expired, so
    the bound asserted here sits well inside grace rather than merely under it.

    Its sibling above asserts that a group member which ignored the first signal
    is still killed. The two pull in opposite directions -- one says do not wait
    for a foreign process, the other says do not stop waiting while the group is
    populated -- and satisfying either alone is easy, which is why they are kept
    together.
    """
    _, elapsed, returncode = _terminate_with_a_descendant(
        escapes_group=True, holds_pipe=True, grace=5.0
    )
    assert returncode is not None, "the child had not been reaped"
    assert elapsed < 1.0, (
        f"cleanup took {elapsed:.2f}s of a 5s grace: it was waiting on the "
        "escaped pipe holder rather than on the child"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_error", "expect_forced_kill"),
    [
        (OSError(errno.EINTR, "Interrupted system call"), True),
        (ProcessLookupError(errno.ESRCH, "No such process"), False),
    ],
    ids=["unreadable-probe", "genuinely-empty"],
)
async def test_an_unreadable_group_probe_does_not_read_as_an_empty_group(
    probe_error, expect_forced_kill
):
    """Only "no such group" is evidence of an empty group.

    Signal 0 can fail for reasons that say nothing about membership. Answering
    "nobody there" to those ends grace early and skips the forced kill, which
    turns the guard off in precisely the situation it exists for, and does so
    silently. The second case is the control: a probe that really does report an
    empty group must still take the quiet path, otherwise this test would pass
    on an implementation that force-kills unconditionally.
    """
    import lionagi.ln._proc as proc_mod

    if not hasattr(os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    class _ExitsPromptly:
        """Models what a real proc reports: returncode is None until it is reaped."""

        def __init__(self):
            self.pid = 7777
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    proc = _ExitsPromptly()
    calls = []

    def _killpg(pgid, sig):
        if sig == 0:
            raise probe_error
        calls.append((pgid, sig))

    with (
        patch.object(proc_mod, "_group_has_witness", return_value=True),
        patch.object(proc_mod.os, "killpg", side_effect=_killpg),
    ):
        await aterminate_process_group(
            proc,
            grace=0.05,
            group_witnesses=((7777, 10.0),),
        )

    assert (7777, signal.SIGTERM) in calls
    assert ((7777, signal.SIGKILL) in calls) is expect_forced_kill, (
        f"probe raised {type(probe_error).__name__} and the forced kill was "
        f"{'skipped' if expect_forced_kill else 'sent'}"
    )


@pytest.mark.asyncio
async def test_a_recycled_group_is_not_sigkilled_after_the_child_exits():
    """A numeric PGID is not proof that the original group still owns it.

    The original leader is captured at start time 10.0 and exits promptly on
    SIGTERM. During the remainder of grace, another process takes the same
    PID/PGID at start time 20.0. A membership-only probe still says that the
    group is populated, but the captured identity no longer matches, so the
    group-wide SIGKILL must be withheld. Direct-child cleanup remains the
    fail-closed fallback.

    This only requires a fresh identity witness immediately before signalling;
    it deliberately does not claim to close the final verify-to-signal race,
    which process-group APIs cannot close portably (see
    docs/internals/ln-primitives.md#signalling-a-group).
    """
    import lionagi.ln._proc as proc_mod

    if not hasattr(os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    proc = SimpleNamespace(pid=7777, returncode=None)

    def _terminate():
        proc.returncode = -signal.SIGTERM

    proc.terminate = MagicMock(side_effect=_terminate)
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=proc.returncode)

    witnesses = ((7777, 10.0),)
    still_owned = MagicMock(side_effect=[True, False])
    calls = []

    def _killpg(pgid, sig):
        calls.append((pgid, sig))

    with (
        patch.object(proc_mod, "_group_has_witness", still_owned),
        patch.object(proc_mod, "_group_has_members", return_value=True),
        patch.object(proc_mod.os, "killpg", side_effect=_killpg),
    ):
        await aterminate_process_group(
            proc,
            grace=0.01,
            group_witnesses=witnesses,
        )

    assert (7777, signal.SIGTERM) in calls
    assert (7777, signal.SIGKILL) not in calls
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert [entry.args for entry in still_owned.call_args_list] == [
        (7777, witnesses),
        (7777, witnesses),
    ]


@pytest.mark.asyncio
async def test_an_unproven_group_uses_only_direct_child_cleanup():
    """No caller-supplied spawn witness means no group-wide signal."""
    import lionagi.ln._proc as proc_mod

    if not hasattr(os, "killpg"):
        pytest.skip("os.killpg not available on this platform")

    proc = _fake_async_proc(pid=8888, wait_delay=10.0)
    late_capture = MagicMock()
    calls = []

    with (
        patch.object(proc_mod, "capture_group_witnesses", late_capture),
        patch.object(
            proc_mod.os, "killpg", side_effect=lambda pgid, sig: calls.append((pgid, sig))
        ),
    ):
        await aterminate_process_group(proc, grace=0.01)

    assert calls == []
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    late_capture.assert_not_called()


@pytest.mark.parametrize(
    "second_anchor",
    [
        ("found", (7777, 20.0)),
        ("gone", None),
    ],
    ids=["leader-identity-changed", "leader-disappeared"],
)
def test_group_witness_capture_requires_the_same_leader_on_both_anchors(
    second_anchor,
):
    """A scan is owned only while one leader identity brackets the whole read."""
    import lionagi.ln._proc as proc_mod

    first_anchor = ("found", (7777, 10.0))
    members = ([(7777, 10.0), (7778, 11.0)], True)
    with (
        patch.object(
            proc_mod,
            "_pinned_group_identity",
            side_effect=[first_anchor, second_anchor],
        ) as pin,
        patch.object(proc_mod, "_group_member_identities", return_value=members) as scan,
    ):
        captured = proc_mod._capture_group_witnesses(SimpleNamespace(pid=7777), 7777)

    assert captured == ()
    assert pin.call_count == 2
    scan.assert_called_once_with(7777)


def test_group_witness_capture_refuses_an_already_reaped_child():
    """A returned process handle cannot anchor whatever later reused its pid."""
    import lionagi.ln._proc as proc_mod

    pin = MagicMock(return_value=("found", (7777, 20.0)))
    scan = MagicMock(return_value=([(7777, 20.0)], True))
    with (
        patch.object(proc_mod, "_pinned_group_identity", pin),
        patch.object(proc_mod, "_group_member_identities", scan),
    ):
        captured = proc_mod._capture_group_witnesses(SimpleNamespace(pid=7777, returncode=0), 7777)

    assert captured == ()
    pin.assert_not_called()
    scan.assert_not_called()


def test_public_group_witness_capture_requires_spawn_time():
    """Teardown-time capture without spawn identity is not a public operation."""
    with pytest.raises(TypeError):
        capture_group_witnesses(SimpleNamespace(pid=7777))  # type: ignore[call-arg]


def test_prepare_process_group_env_adds_a_fresh_private_marker():
    """The identity fact exists before spawn without mutating caller state."""
    base = {"PATH": "/bin"}

    first_env, first_marker = prepare_process_group_env(base)
    second_env, second_marker = prepare_process_group_env(base)

    assert base == {"PATH": "/bin"}
    assert first_env["PATH"] == "/bin"
    assert first_marker in first_env.values()
    assert second_marker in second_env.values()
    assert first_marker != second_marker


def test_spawn_marker_rejects_a_pid_reused_before_the_first_probe():
    """A consistent live stranger cannot manufacture the pre-spawn fact."""
    import lionagi.ln._proc as proc_mod

    stranger = (7777, 20.0, "stranger-marker", True)
    with (
        patch.object(proc_mod.os, "getpgid", create=True),
        patch.object(proc_mod, "pinned_member", return_value=("found", stranger)) as pin,
    ):
        created = observe_process_group_leader(
            7777,
            expected_marker="our-marker",
        )

    assert created is None
    pin.assert_called_once_with(
        7777,
        7777,
        marker_var=proc_mod._PROCESS_GROUP_MARKER,
    )


def test_spawn_marker_accepts_the_bracketed_original_leader():
    """A matching injected marker yields the create time frozen for teardown."""
    import lionagi.ln._proc as proc_mod

    original = (7777, 10.0, "our-marker", True)
    with (
        patch.object(proc_mod.os, "getpgid", create=True),
        patch.object(proc_mod, "pinned_member", return_value=("found", original)),
    ):
        created = observe_process_group_leader(
            7777,
            expected_marker="our-marker",
        )

    assert created == 10.0


def test_group_witness_revalidation_rejects_a_reused_pid():
    """The same PID and PGID with a new start time is a different group member."""
    import lionagi.ln._proc as proc_mod

    probe = MagicMock(return_value=("found", (7777, 20.0)))
    with patch.object(proc_mod, "_pinned_group_identity", probe):
        matched = proc_mod._group_has_witness(7777, ((7777, 10.0),))

    assert matched is False
    probe.assert_called_once_with(7777, 7777)


def test_a_reaped_leader_anchors_nothing_when_the_group_scan_finds_no_member():
    """A freed PGID must not be signalled on a bare liveness probe.

    When the marker scan returns nothing, capture falls back to anchoring the
    group by its number and revalidating with ``killpg(pgid, 0)`` before
    signalling. That is sound only while the leader is alive, because a live
    leader is what keeps the PGID from being reissued. Once the leader has been
    reaped the group can be empty and the number handed to something else, and
    the probe would then answer for whoever holds it now -- which is authority
    to SIGKILL an unrelated process group.
    """
    import lionagi.ln._proc as proc_mod

    with patch.object(proc_mod, "live_group_members", MagicMock(return_value=([], False))):
        captured = capture_group_witnesses(
            SimpleNamespace(pid=7777, returncode=0),
            expected_leader_created=20.0,
            expected_marker="marker-value",
        )

    assert captured == ()
    assert not getattr(captured, "group_anchor", False)


def test_a_live_leader_still_anchors_its_group_when_the_scan_is_unreadable():
    """The must-match half: an unreadable scan must not disarm a live group.

    Some environments deny a process listing even for this user's own
    processes. While the leader is still running it holds the PGID, so nothing
    else can be occupying it and the anchor remains real evidence.
    """
    import lionagi.ln._proc as proc_mod

    with patch.object(proc_mod, "live_group_members", MagicMock(return_value=([], False))):
        captured = capture_group_witnesses(
            SimpleNamespace(pid=7777, returncode=None),
            expected_leader_created=20.0,
            expected_marker="marker-value",
        )

    assert tuple(captured) == ((7777, 20.0),)
    assert getattr(captured, "group_anchor", False) is True


def test_signalling_needs_a_matching_witness_and_not_only_a_live_pgid():
    """Signalling authority comes from the witness set, never from liveness.

    A witness set carrying the group id but no longer matching any live member
    used to escalate through ``killpg(pgid, 0)``. That path could only be
    reached once the real members were gone, which is exactly when the number
    is free to be reused.
    """
    import lionagi.ln._proc as proc_mod

    killpg = MagicMock()
    with (
        patch.object(proc_mod, "_group_has_witness", MagicMock(return_value=False)),
        patch.object(proc_mod.os, "killpg", killpg),
    ):
        signalled = proc_mod.signal_group_if_witnessed(7777, ((7777, 20.0),), signal.SIGKILL)

    assert signalled is False
    killpg.assert_not_called()


def test_an_anchor_stops_authorizing_a_kill_once_its_leader_exits():
    """The anchor is re-checked where it is SPENT, not only where it is minted.

    Capture refuses to mint an anchor for an already-reaped leader, and that
    guard is real, but it cannot answer this question: an anchor exists in
    order to be used later, and "later" is exactly when the leader gets
    reaped. Between capture and signalling the leader can exit, the group can
    empty out, and the kernel is then free to hand the number to something
    else -- at which point ``killpg(pgid, 0)`` succeeds for a completely
    unrelated group and reads as proof of ownership.
    """
    import lionagi.ln._proc as proc_mod

    leader = SimpleNamespace(pid=7777, returncode=None)
    with patch.object(proc_mod, "live_group_members", MagicMock(return_value=([], False))):
        captured = capture_group_witnesses(
            leader,
            expected_leader_created=20.0,
            expected_marker="marker-value",
        )
    assert getattr(captured, "group_anchor", False) is True

    # The leader exits after the anchor was captured.
    leader.returncode = 0

    killpg = MagicMock()
    with (
        patch.object(proc_mod, "_group_has_witness", MagicMock(return_value=False)),
        patch.object(proc_mod.os, "killpg", killpg),
    ):
        signalled = proc_mod.signal_group_if_witnessed(7777, captured, signal.SIGKILL)

    assert signalled is False
    # Not even the liveness probe: asking at all is what turns a recycled PGID
    # into an answer.
    killpg.assert_not_called()


def test_an_anchor_still_authorizes_a_kill_while_its_leader_is_live():
    """The must-match half, so the guard above cannot pass by refusing everything.

    While the leader is running it holds the PGID and nothing else can be
    occupying it, so the anchor is real evidence and the force-kill path has to
    stay available -- that path is the only thing that ends a group whose
    process listing this platform will not produce.
    """
    import lionagi.ln._proc as proc_mod

    leader = SimpleNamespace(pid=7777, returncode=None)
    with patch.object(proc_mod, "live_group_members", MagicMock(return_value=([], False))):
        captured = capture_group_witnesses(
            leader,
            expected_leader_created=20.0,
            expected_marker="marker-value",
        )
    assert getattr(captured, "group_anchor", False) is True

    killpg = MagicMock()
    with (
        patch.object(proc_mod, "_group_has_witness", MagicMock(return_value=False)),
        patch.object(proc_mod.os, "killpg", killpg),
    ):
        signalled = proc_mod.signal_group_if_witnessed(7777, captured, signal.SIGKILL)

    assert signalled is True
    assert killpg.call_args_list == [call(7777, 0), call(7777, signal.SIGKILL)]


# The direct child's own signal never depends on the group signal succeeding.


class _GroupSignalBroke(Exception):
    pass


def _group_signal_that_raises_on(*raising: signal.Signals):
    sent = []

    def signal_group(pgid, witnesses, sig):
        sent.append(sig)
        if sig in raising:
            raise _GroupSignalBroke(sig)
        return True

    return signal_group, sent


def test_sync_kill_reaches_the_child_when_the_group_signal_raises(monkeypatch):
    import lionagi.ln._proc as proc_mod

    signal_group, sent = _group_signal_that_raises_on(signal.SIGKILL)
    monkeypatch.setattr(proc_mod, "signal_group_if_witnessed", signal_group)
    proc = _fake_proc(pid=4242)

    with pytest.raises(_GroupSignalBroke):
        terminate_process_group(proc, grace=None, group_witnesses=((4242, 10.0),))

    assert sent == [signal.SIGKILL]
    proc.kill.assert_called_once()


def test_sync_terminate_reaches_the_child_when_the_group_signal_raises(monkeypatch):
    import lionagi.ln._proc as proc_mod

    signal_group, sent = _group_signal_that_raises_on(signal.SIGTERM)
    monkeypatch.setattr(proc_mod, "signal_group_if_witnessed", signal_group)
    proc = _fake_proc(pid=4242)

    with pytest.raises(_GroupSignalBroke):
        terminate_process_group(proc, grace=5.0, group_witnesses=((4242, 10.0),))

    assert sent == [signal.SIGTERM]
    proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_async_kill_reaches_the_child_when_the_group_signal_raises(monkeypatch):
    import lionagi.ln._proc as proc_mod

    signal_group, sent = _group_signal_that_raises_on(signal.SIGKILL)
    monkeypatch.setattr(proc_mod, "signal_group_if_witnessed", signal_group)
    proc = _fake_async_proc(pid=4242)

    with pytest.raises(_GroupSignalBroke):
        await aterminate_process_group(proc, grace=None, group_witnesses=((4242, 10.0),))

    assert sent == [signal.SIGKILL]
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_async_terminate_reaches_the_child_when_the_group_signal_raises(monkeypatch):
    import lionagi.ln._proc as proc_mod

    signal_group, sent = _group_signal_that_raises_on(signal.SIGTERM)
    monkeypatch.setattr(proc_mod, "signal_group_if_witnessed", signal_group)
    proc = _fake_async_proc(pid=4242)
    proc.returncode = None

    with pytest.raises(_GroupSignalBroke):
        await aterminate_process_group(proc, grace=0.05, group_witnesses=((4242, 10.0),))

    # A child that ignores the first signal is killed when the grace runs out; the
    # group is not signalled again.
    assert sent == [signal.SIGTERM]
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_async_terminate_after_a_raised_group_signal_waits_on_the_child_alone(monkeypatch):
    import lionagi.ln._proc as proc_mod

    signal_group, sent = _group_signal_that_raises_on(signal.SIGTERM)
    monkeypatch.setattr(proc_mod, "signal_group_if_witnessed", signal_group)
    group_reads = []
    monkeypatch.setattr(
        proc_mod, "_group_has_members", lambda pgid: group_reads.append(pgid) or True
    )
    proc = _fake_async_proc(pid=4242)
    proc.returncode = None

    def exits(*_args):
        proc.returncode = -signal.SIGTERM

    proc.terminate.side_effect = exits

    with pytest.raises(_GroupSignalBroke):
        await aterminate_process_group(proc, grace=5.0, group_witnesses=((4242, 10.0),))

    assert sent == [signal.SIGTERM]
    assert group_reads == []
    proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_async_escalation_kills_the_child_when_the_group_kill_raises(monkeypatch):
    """The first signal is ignored, the grace runs out, and the group SIGKILL raises."""
    import lionagi.ln._proc as proc_mod

    signal_group, sent = _group_signal_that_raises_on(signal.SIGKILL)
    monkeypatch.setattr(proc_mod, "signal_group_if_witnessed", signal_group)
    proc = _fake_async_proc(pid=4242)
    proc.returncode = None

    with pytest.raises(_GroupSignalBroke):
        await aterminate_process_group(proc, grace=0.05, group_witnesses=((4242, 10.0),))

    assert sent == [signal.SIGTERM, signal.SIGKILL]
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
