# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import re
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

_SHELL_CONTROL = re.compile(r"(;|&&|\|\||\||`|\$\(|[<>]|\n)")

_MAX_OUTPUT_BYTES = 100_000
_CLEANUP_GRACE_SECONDS = 1.0


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
        # keep draining even after cap to avoid deadlocking the child process
    return truncated


def _decode_output(buf: bytearray, truncated: bool) -> str:
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

    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
    else:
        t_out.join(timeout=max(0.0, deadline - time.monotonic()))
        t_err.join(timeout=max(0.0, deadline - time.monotonic()))
        timed_out = t_out.is_alive() or t_err.is_alive()

    if timed_out:
        # A surviving descendant can still pin the group after the leader exits.
        # Require the spawn marker to distinguish it from a reused PGID.
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
        return {
            "stdout": _decode_output(stdout_buf, True),
            "stderr": f"Command timed out after {label}",
            "returncode": -1,
            "timed_out": True,
        }

    return {
        "stdout": _decode_output(stdout_buf, stdout_truncated[0]),
        "stderr": _decode_output(stderr_buf, stderr_truncated[0]),
        "returncode": proc.returncode,
    }
