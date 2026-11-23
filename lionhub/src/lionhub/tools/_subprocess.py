# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import io
import re
import select
import subprocess
import threading
import time
from collections.abc import Mapping

from lionagi._command_env_metadata import command_environment
from lionagi.ln import (
    capture_group_witnesses,
    observe_process_group_leader,
    prepare_process_group_env,
    terminate_process_group,
)

# The real class, kept before anything can replace the module attribute: only a child this
# module spawned with it is waited on without being collected.
_POPEN = subprocess.Popen
_SHELL_CONTROL = re.compile(r"(;|&&|\|\||\||`|\$\(|[<>]|\n)")

_MAX_OUTPUT_BYTES = 100_000
_CLEANUP_GRACE_SECONDS = 1.0
# After a command exits inside its deadline, the pipe readers get at least this long to take
# the output already written, so a reader slowed by load is not reported as a timeout.
_DRAIN_GRACE_SECONDS = 1.0


def _wait_for_exit(proc: subprocess.Popen, timeout: float) -> bool:
    """Whether *proc* exited within *timeout*, leaving it unreaped where the platform allows.

    On a kqueue platform (macOS) the exit is observed without collecting the child, so its pid,
    and the process group it leads, stay reserved until the caller decides whether to end the
    group. That is what lets a group whose members cannot show their spawn marker (a protected
    system binary on macOS) still be ended after its leader has exited. Elsewhere this waits
    with ``Popen.wait``, which collects the child; a member's marker is readable there. It also
    waits that way when kqueue refuses the watch for a reason other than the process having
    exited.
    """
    kqueue = getattr(select, "kqueue", None)
    if kqueue is not None and isinstance(proc, _POPEN):
        queue = kqueue()
        try:
            exit_event = select.kevent(
                proc.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            events = queue.control([exit_event], 1, timeout)
        finally:
            queue.close()
        if not events:
            return False
        # A refused watch comes back as an event with EV_ERROR set and the errno in ``data``.
        # ESRCH means the kernel no longer lists the process as running: it has exited.
        # Any other errno says nothing about the child.
        event = events[0]
        if not event.flags & select.KQ_EV_ERROR or event.data == errno.ESRCH:
            return True
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _collect(proc: subprocess.Popen) -> None:
    """Collect a child that has already exited; bounded, like every wait here."""
    try:
        proc.wait(timeout=_CLEANUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _drain(stream, buf: bytearray) -> bool:
    truncated = False
    # read1 returns what the pipe holds now instead of waiting for a full chunk,
    # so output printed before a deadline is kept even when the pipe never closes.
    read = stream.read1 if isinstance(stream, io.BufferedIOBase) else stream.read
    while True:
        try:
            chunk = read(8192)
        except Exception:
            break
        if not chunk:
            break
        remaining = _MAX_OUTPUT_BYTES - len(buf)
        if remaining > 0:
            buf.extend(chunk[:remaining])
            if len(buf) >= _MAX_OUTPUT_BYTES:
                truncated = True
                # Nothing more is kept, so full-chunk reads are enough to keep the pipe drained.
                read = stream.read
        # keep draining even after cap to avoid deadlocking the child process
    return truncated


def _decode_output(buf: bytes | bytearray, truncated: bool) -> str:
    text = bytes(buf).decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n[... output truncated at {_MAX_OUTPUT_BYTES} bytes ...]\n"
    return text


def _subprocess_sync(
    cmd: str | list[str],
    shell: bool,
    timeout_sec: float,
    cwd: str | None,
    timeout_ms: int | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Run a coding command with a scrubbed default or an explicitly supplied mapping."""
    child_env, group_marker = prepare_process_group_env(
        command_environment() if env is None else env
    )
    try:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or None,
            env=child_env,
            start_new_session=True,
        )
    except Exception as e:
        return {"stdout": "", "stderr": f"Execution error: {e}", "returncode": -1}

    deadline = time.monotonic() + timeout_sec
    leader_created = observe_process_group_leader(
        getattr(proc, "pid", None),
        expected_marker=group_marker,
    )

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]

    def _drain_stdout():
        stdout_truncated[0] = _drain(proc.stdout, stdout_buf)

    def _drain_stderr():
        stderr_truncated[0] = _drain(proc.stderr, stderr_buf)

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()

    if not _wait_for_exit(proc, max(0.0, deadline - time.monotonic())):
        timed_out = True
    else:
        # A descendant that keeps a pipe open is bounded by the deadline, or by the drain grace
        # after the exit when the command exits less than that before the deadline, so output
        # written before an exit just inside the deadline is still collected.
        drain_deadline = max(deadline, time.monotonic() + _DRAIN_GRACE_SECONDS)
        t_out.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        t_err.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        timed_out = t_out.is_alive() or t_err.is_alive()

    if timed_out:
        # A surviving descendant can still pin the group after the leader exits.
        # Require the spawn marker to distinguish it from a reused PGID, or, where
        # no member shows it, the leader still unreaped.
        group_witnesses = (
            capture_group_witnesses(
                proc,
                expected_leader_created=leader_created,
                expected_marker=group_marker,
            )
            if leader_created is not None
            else ()
        )
        terminate_process_group(
            proc,
            grace=None,
            group_witnesses=group_witnesses,
        )
        cleanup_deadline = time.monotonic() + _CLEANUP_GRACE_SECONDS
        try:
            proc.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        # A pipe held independently of the child may never close. Leave its
        # daemon drain running when the cleanup grace has elapsed.
        t_out.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        t_err.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        label = f"{timeout_ms} ms" if timeout_ms is not None else f"{timeout_sec}s"
        # The drain may still be running, so read the cap from the buffer itself.
        stdout = bytes(stdout_buf)
        return {
            "stdout": _decode_output(stdout, len(stdout) >= _MAX_OUTPUT_BYTES),
            "stderr": f"Command timed out after {label}",
            "returncode": -1,
            "timed_out": True,
        }

    # Where the exit was observed without collecting the child, collect it now.
    if proc.returncode is None:
        _collect(proc)
    return {
        "stdout": _decode_output(stdout_buf, stdout_truncated[0]),
        "stderr": _decode_output(stderr_buf, stderr_truncated[0]),
        "returncode": proc.returncode,
    }
