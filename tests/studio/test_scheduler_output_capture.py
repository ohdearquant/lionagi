# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""What a failed scheduled run leaves on the record.

``error_detail`` is the only thing a dashboard can show an operator about a
failure, and it is filled from what ``spawn_and_wait`` returns. A command that
reported its failure on stdout used to leave a bare exit code there.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock

import pytest

from lionagi.studio.scheduler.subprocess import _TAIL_BYTES, spawn_and_wait


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


async def test_a_failure_reported_on_stdout_reaches_the_record():
    """The reported defect: exit 1 with the diagnostic on stdout, and nothing kept."""
    code, tail = await spawn_and_wait(
        _py("import sys; print('tenant 42 has no owner'); sys.exit(1)"),
        "inv-stdout",
    )
    assert code == 1
    assert "tenant 42 has no owner" in tail
    # Labelled, because which stream said it tells a reader where to look next.
    assert "[stdout]" in tail


async def test_a_stderr_only_failure_is_unchanged_byte_for_byte():
    """Runs that already record useful tracebacks must not shift at all.

    This is what bounds the change: rows that work today are untouched, and the
    difference is confined to rows that carried nothing or half the story.
    """
    code, tail = await spawn_and_wait(
        _py("import sys; sys.stderr.write('Traceback: boom\\n'); sys.exit(1)"),
        "inv-stderr",
    )
    assert code == 1
    assert tail == "Traceback: boom\n"
    assert "[stdout]" not in tail


async def test_both_streams_are_kept_and_attributed():
    code, tail = await spawn_and_wait(
        _py(
            "import sys; sys.stderr.write('err line\\n'); "
            "sys.stdout.write('out line\\n'); sys.exit(2)"
        ),
        "inv-both",
    )
    assert code == 2
    assert "err line" in tail
    assert "out line" in tail
    # stderr first: it is still the conventional error channel, and a reader
    # scanning the head of the field should meet it before the addition.
    assert tail.index("err line") < tail.index("[stdout]")


async def test_stdout_whitespace_alone_does_not_add_a_label():
    """A command printing a trailing newline has not said anything."""
    code, tail = await spawn_and_wait(
        _py("import sys; print(); sys.stderr.write('real error\\n'); sys.exit(1)"),
        "inv-blank",
    )
    assert code == 1
    assert tail == "real error\n"


async def test_a_child_that_outproduces_both_pipe_buffers_does_not_deadlock():
    """The trap in the obvious fix.

    An OS pipe buffer is tens of kilobytes. A child filling one blocks until
    someone reads it, so draining the streams one after the other hangs forever
    on any process that writes enough to both — every streaming agent leg. The
    timeout is the assertion: without concurrent draining this never returns.
    """
    big = 400_000
    code, tail = await asyncio.wait_for(
        spawn_and_wait(
            _py(
                f"import sys; sys.stdout.write('o' * {big}); "
                f"sys.stderr.write('e' * {big}); sys.exit(3)"
            ),
            "inv-flood",
        ),
        timeout=60,
    )
    assert code == 3
    # Bounded on the way in, so a leg streaming for an hour cannot grow the
    # scheduler's memory with output nobody will read.
    assert len(tail) < 2 * _TAIL_BYTES + 64
    assert "o" in tail and "e" in tail


async def test_the_retained_slice_is_the_tail_not_the_head():
    """A traceback's last lines are the ones that name the failure."""
    code, tail = await spawn_and_wait(
        _py(
            "import sys; sys.stderr.write('X' * 5000); "
            "sys.stderr.write('THE ACTUAL ERROR'); sys.exit(1)"
        ),
        "inv-tail",
    )
    assert code == 1
    assert tail.endswith("THE ACTUAL ERROR")
    assert len(tail) <= _TAIL_BYTES


async def test_a_cancelled_spawn_still_terminates_the_child():
    """Cancellation kills the tree; the readers must not outlive it.

    The drain tasks are blocked on pipes belonging to the process being killed,
    so an un-cancelled reader would keep the coroutine alive past the cancel.
    """
    task = asyncio.create_task(spawn_and_wait(_py("import time; time.sleep(120)"), "inv-cancel"))
    await asyncio.sleep(0.5)  # let it reach the drain
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=30)


async def test_a_deadline_terminates_the_owned_process_group(monkeypatch):
    """Deadline expiry follows the same process-tree cleanup contract as cancellation."""
    from lionagi.studio.scheduler import subprocess as subprocess_mod

    class _BlockingStream:
        async def read(self, _size: int) -> bytes:
            await asyncio.Event().wait()
            return b""  # pragma: no cover - unreachable

    class _FakeProc:
        pid = 424244
        returncode = None
        stdout = _BlockingStream()
        stderr = _BlockingStream()

        async def wait(self):
            await asyncio.Event().wait()

    proc = _FakeProc()

    async def _fake_exec(*_args, **kwargs):
        assert kwargs["start_new_session"] is True
        return proc

    cleanup = AsyncMock()
    monkeypatch.setattr(subprocess_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(subprocess_mod, "aterminate_process_group", cleanup)

    with pytest.raises(subprocess_mod.SubprocessDeadlineExceededError, match="0.01"):
        await subprocess_mod.spawn_and_wait(
            ["sleep", "30"],
            "inv-deadline",
            deadline_seconds=0.01,
        )

    cleanup.assert_awaited_once_with(proc, grace=5.0)


async def test_a_process_finishing_before_its_deadline_is_unchanged():
    code, tail = await spawn_and_wait(
        _py("print('done')"),
        "inv-before-deadline",
        deadline_seconds=5,
    )
    assert code == 0
    assert "done" in tail


async def test_the_deadline_includes_post_spawn_dispatch_confirmation():
    """A stuck durability callback cannot leave the already-running child unbounded."""
    from lionagi.studio.scheduler import subprocess as subprocess_mod

    async def _never_confirms():
        await asyncio.Event().wait()

    with pytest.raises(subprocess_mod.SubprocessDeadlineExceededError):
        await asyncio.wait_for(
            subprocess_mod.spawn_and_wait(
                _py("import time; time.sleep(120)"),
                "inv-callback-deadline",
                deadline_seconds=0.05,
                on_launched=_never_confirms,
            ),
            timeout=2,
        )


async def test_a_non_positive_deadline_is_rejected_before_spawn(monkeypatch):
    from lionagi.studio.scheduler import subprocess as subprocess_mod

    create = AsyncMock()
    monkeypatch.setattr(subprocess_mod.asyncio, "create_subprocess_exec", create)

    with pytest.raises(ValueError, match="positive"):
        await subprocess_mod.spawn_and_wait(
            [sys.executable, "-c", "pass"],
            "inv-invalid-deadline",
            deadline_seconds=0,
        )
    create.assert_not_awaited()


async def test_a_non_positive_per_kind_deadline_is_rejected_before_spawn(monkeypatch):
    from lionagi.studio.scheduler import subprocess as subprocess_mod

    create = AsyncMock()
    monkeypatch.setenv("LIONAGI_STUDIO_INVOCATION_DEADLINE_AGENT_SECONDS", "-1")
    monkeypatch.setattr(subprocess_mod.asyncio, "create_subprocess_exec", create)

    with pytest.raises(ValueError, match="must be positive"):
        await subprocess_mod.spawn_and_wait(
            [sys.executable, "-c", "pass"],
            "inv-invalid-agent-deadline",
            action_kind="agent",
        )
    create.assert_not_awaited()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
async def test_a_deadline_kills_a_real_descendant_process(tmp_path, monkeypatch):
    """Returning from a timeout means no descendant remains in the owned group."""
    from lionagi.studio.scheduler import subprocess as subprocess_mod

    pid_file = tmp_path / "descendant.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(120)"
    )
    monkeypatch.setattr(subprocess_mod, "_PROCESS_TERMINATION_GRACE_SECONDS", 0.25)

    with pytest.raises(subprocess_mod.SubprocessDeadlineExceededError):
        await subprocess_mod.spawn_and_wait(
            _py(script),
            "inv-descendant-deadline",
            deadline_seconds=0.25,
        )

    descendant_pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - diagnostic path
        pytest.fail(f"descendant {descendant_pid} survived process-group deadline cleanup")
