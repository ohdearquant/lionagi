# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Background job engine for the lionagi MCP server.

``submit()`` spawns a ``li`` command as a detached process and returns immediately
with the run_id. The id is pre-assigned via ``LIONAGI_RUN_ID`` so it is known
before the child starts (no polling to discover it). ``status()`` / ``output()`` /
``kill()`` / ``list_jobs()`` then operate on that id by reading the run state the
CLI persists plus the MCP server's own small per-job record.

The detached child gets its own session/pgid (``start_new_session``), so it
survives an MCP-server restart and can still be signalled as a group. That is why
job state lives on disk rather than in server memory.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import config

# li subcommand for each job kind. "orchestrate" is the canonical parser name
# (the `o` alias also works); flow and fanout live under it.
_KIND_ARGV: dict[str, list[str]] = {
    "agent": ["agent"],
    "flow": ["orchestrate", "flow"],
    "fanout": ["orchestrate", "fanout"],
}

_TERMINAL_STATES = {"completed", "failed", "killed", "timeout", "exited"}

# The terminal hook module, invoked by the CLI's --notify by absolute
# interpreter path so it runs regardless of PATH in the CLI's environment.
_NOTIFY_MODULE = "lionagi.mcp._notify_hook"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """Mint a run_id in the CLI's own format: ``YYYYMMDDTHHMMSS-<6hex>``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid4().hex[:6]}"


# --- record I/O ----------------------------------------------------------------


def _write_job(record: dict[str, Any]) -> None:
    d = config.job_dir(record["run_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps(record, indent=2))


def _read_job(run_id: str) -> dict[str, Any] | None:
    p = config.job_dir(run_id) / "job.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _read_run_manifest(run_id: str) -> dict[str, Any] | None:
    p = config.run_manifest(run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --- process + log helpers -----------------------------------------------------


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 1:
        return False
    # A detached child is still OUR child, so once it exits unreaped it lingers
    # as a zombie and `kill -0` would report it alive. Reap it first: waitpid
    # returns (pid, _) if it just exited, (0, 0) if still running, and raises
    # ChildProcessError when it is not our child (e.g. after an MCP-server
    # restart, where init reaps it and a direct probe is authoritative).
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
        if reaped == 0:
            return True
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tail(path: str | None, limit: int = 4000) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = p.read_text(errors="replace")
    except OSError:
        return None
    return data[-limit:] if len(data) > limit else data


def _list_artifacts(run_id: str) -> list[str]:
    adir = config.run_dir(run_id) / "artifacts"
    if not adir.exists():
        return []
    return sorted(str(p.relative_to(adir)) for p in adir.rglob("*") if p.is_file())


def _notify_template(run_id: str, notify_target: str | None, notify_command: str | None) -> str:
    """Command the CLI runs on terminal status (records finished_at + delivery).

    Invokes the terminal hook module by absolute interpreter path with a
    ``{status}`` placeholder the CLI substitutes (a bareword, so it survives
    the CLI's own shlex-split before being replaced). ``--target`` carries the
    ``{target}`` value; ``--command`` carries an optional per-submit delivery
    override.
    """
    parts = [
        shlex.quote(sys.executable),
        "-m",
        _NOTIFY_MODULE,
        "--run-id",
        shlex.quote(run_id),
        "--status",
        "{status}",
    ]
    if notify_target:
        parts += ["--target", shlex.quote(notify_target)]
    if notify_command:
        parts += ["--command", shlex.quote(notify_command)]
    return " ".join(parts)


# --- public API ----------------------------------------------------------------


def submit(
    kind: str,
    flags: list[str],
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    label: str | None = None,
    notify_command: str | None = None,
    notify_target: str | None = None,
) -> dict[str, Any]:
    """Spawn a ``li`` run in the background and return its handle immediately.

    *flags* are the already-built CLI flags (everything except the prompt).
    *prompt*, when given, is handed to an agent via ``--prompt-file`` (robust for
    long text) or appended as the flow/fanout positional.

    On terminal, the run records its status and — if a delivery command is
    configured — sends a terminal notice. *notify_command* is an optional
    per-submit delivery-argv override (JSON list); *notify_target* fills the
    ``{target}`` placeholder in the configured command. With neither and no
    configured default, the run simply records its status and delivers nothing.
    """
    if kind not in _KIND_ARGV:
        raise ValueError(f"unknown job kind {kind!r}; expected one of {sorted(_KIND_ARGV)}")

    run_id = new_run_id()
    d = config.job_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / "console.log"

    tail = list(flags)
    if prompt is not None:
        if kind == "agent":
            pf = d / "prompt.txt"
            pf.write_text(prompt)
            tail += ["--prompt-file", str(pf)]
        else:
            tail.append(prompt)  # flow/fanout take the prompt as a positional

    # Wire the CLI's terminal hook back to the MCP server so we record a reliable
    # finished_at/status (and fire the configured delivery) even across a restart.
    tail = ["--notify", _notify_template(run_id, notify_target, notify_command), *tail]

    argv = [*config.li_command(), *_KIND_ARGV[kind], *tail]

    # Drop the parent harness marker so the detached child does not inherit an
    # environment that claims it is running under an interactive harness.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env[config.RUN_ID_ENV_VAR] = run_id

    log_f = open(log_path, "wb")
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is the resolved li_command + CLI flags, no shell
            argv,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd or None,
            env=env,
            start_new_session=True,  # own session/pgid: survives restart, killable as a group
        )
    finally:
        log_f.close()  # child holds its own fd; parent drops its copy

    record = {
        "run_id": run_id,
        "pid": proc.pid,
        "kind": kind,
        "argv": argv,
        "cwd": cwd,
        "label": label,
        "notify_command": notify_command,
        "notify_target": notify_target,
        "submitted_at": _now_iso(),
        "finished_at": None,
        "status": "running",
        "log": str(log_path),
    }
    _write_job(record)

    return {"run_id": run_id, "pid": proc.pid, "status": "running", "log": str(log_path)}


def status(run_id: str) -> dict[str, Any]:
    """Current state of *run_id*.

    ``status`` is the single authoritative field (the MCP record's terminal
    status, corrected by pid liveness). ``run`` is the raw CLI manifest,
    advisory only — its own ``status`` stays ``running`` until the CLI finalizes
    it in the StateDB, so read ``status`` here, not ``run["status"]``.
    ``notify_delivery`` reports whether the terminal notice was delivered.
    """
    job = _read_job(run_id)
    manifest = _read_run_manifest(run_id)
    pid = job.get("pid") if job else None
    alive = _pid_alive(pid)

    recorded = (job or {}).get("status", "unknown")
    if alive:
        state = "running"
    elif recorded in _TERMINAL_STATES:
        state = recorded  # authoritative terminal from the notify hook
    elif job is not None:
        state = "exited"  # pid gone, no terminal record captured
    else:
        state = "unknown"

    return {
        "run_id": run_id,
        "kind": (job or {}).get("kind"),
        "label": (job or {}).get("label"),
        "status": state,
        "alive": alive,
        "pid": pid,
        "submitted_at": (job or {}).get("submitted_at"),
        "finished_at": (job or {}).get("finished_at"),
        "notify_delivery": (job or {}).get("notify_delivery"),
        "run": manifest,
        "log_tail": _tail((job or {}).get("log")),
        "known": job is not None,
    }


def output(run_id: str, tail_chars: int = 20000) -> dict[str, Any]:
    """Terminal output of *run_id*: the console (an agent's final response prints
    here) plus any persisted artifacts."""
    job = _read_job(run_id)
    if job is None:
        return {"run_id": run_id, "known": False, "error": "no such job"}
    st = status(run_id)
    return {
        "run_id": run_id,
        "known": True,
        "status": st["status"],
        "console": _tail(job.get("log"), limit=tail_chars),
        "artifacts": _list_artifacts(run_id),
        "run_dir": str(config.run_dir(run_id)),
    }


def kill(run_id: str, sig: int = signal.SIGTERM) -> dict[str, Any]:
    """Signal the whole process group of *run_id*."""
    job = _read_job(run_id)
    if job is None:
        return {"run_id": run_id, "killed": False, "reason": "no such job"}
    pid = job.get("pid")
    if not pid or pid <= 1:  # never signal pgid 0/1 (self/init)
        return {"run_id": run_id, "killed": False, "reason": "no pid on record"}
    if not _pid_alive(pid):
        return {"run_id": run_id, "killed": False, "reason": "already exited"}

    reason: str | None = None
    try:
        os.killpg(os.getpgid(pid), sig)
        killed = True
    except ProcessLookupError:
        killed, reason = False, "process gone"
    except PermissionError as e:
        killed, reason = False, f"permission denied: {e}"

    if killed:
        job["status"] = "killed"
        job["finished_at"] = _now_iso()
        _write_job(job)
    return {"run_id": run_id, "killed": killed, "reason": reason, "pid": pid}


def list_jobs(limit: int = 50, status_filter: str | None = None) -> list[dict[str, Any]]:
    """Recent jobs, newest first (run_id sorts by timestamp)."""
    if not config.JOBS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(config.JOBS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        st = status(d.name)
        if status_filter and st["status"] != status_filter:
            continue
        out.append(
            {
                "run_id": st["run_id"],
                "kind": st["kind"],
                "label": st["label"],
                "status": st["status"],
                "submitted_at": st["submitted_at"],
                "finished_at": st["finished_at"],
            }
        )
        if len(out) >= limit:
            break
    return out


def mark_terminal(run_id: str, cli_status: str) -> dict[str, Any] | None:
    """Record a terminal status for *run_id* (called by the CLI notify hook)."""
    job = _read_job(run_id)
    if job is None:
        return None
    job["status"] = cli_status if cli_status in _TERMINAL_STATES else "completed"
    job["cli_status"] = cli_status
    job["finished_at"] = _now_iso()
    _write_job(job)
    return job


def record_notify_delivery(run_id: str, outcome: dict[str, Any]) -> None:
    """Record whether the terminal notice was delivered (called by the notify hook).

    Surfaced by ``status`` so a completion notice that failed to send is visible
    rather than silently lost — the detached-spawn pattern relies on that signal.
    """
    job = _read_job(run_id)
    if job is None:
        return
    job["notify_delivery"] = outcome
    _write_job(job)
