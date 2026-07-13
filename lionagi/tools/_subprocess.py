# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Mapping

from lionagi.ln._proc import terminate_process_group

_SHELL_CONTROL = re.compile(r"(;|&&|\|\||\||`|\$\(|[<>]|\n)")

_MAX_OUTPUT_BYTES = 100_000


def _drain(stream, buf: bytearray) -> bool:
    truncated = False
    while True:
        try:
            chunk = stream.read(8192)
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
    """Run ``cmd`` synchronously; ``env=None`` inherits the parent environment,
    an explicit mapping scopes less-trusted commands to a minimal one."""
    try:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or None,
            env=env,
            start_new_session=True,
        )
    except Exception as e:
        return {"stdout": "", "stderr": f"Execution error: {e}", "returncode": -1}

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

    timed_out = False
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        terminate_process_group(proc, grace=None)
        proc.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        timed_out = True

    if not timed_out:
        t_out.join()
        t_err.join()

    if timed_out:
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
