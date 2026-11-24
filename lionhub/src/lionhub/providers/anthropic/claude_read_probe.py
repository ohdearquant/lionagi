# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Whether Claude Code's sandbox can deny reads by default, for one ``claude`` binary.

ADR-0041 Amendment 7 lets a profile name paths an agent's child may not read, and leaves a list of
the paths it may read until some route has been measured denying reads by default. This probe
measures Claude Code: settings whose ``sandbox.filesystem`` denies ``/`` for reading and allows
reading a set of read roots. The read roots are one directory of the probe's own, the probe's
working directory, the system set lionagi's own Seatbelt policy reads from, each link on the way
to the interpreter the ``python`` step starts that lies outside those, and Claude Code's own temp
directory for the run.

Each run sets ``CLAUDE_CODE_TMPDIR`` to a directory of its own under the probe's root, and Claude
Code keeps its internal temp files in ``claude-<uid>`` below it. The run with read rules grants
that directory and no other temp directory. Without the grant a command cannot inspect its own
output file, and ``/bin/cat``, which does so before copying, fails on every file it opens. The
``tmpdir`` step prints the ``TMPDIR`` Bash gives a command, and ``bash_temp_in_granted_dir``
passes only when it lies in the granted directory: Claude Code is documented to fall back to the
system default under a long override path, so a long ``--scratch`` shows there. The links are
granted one by one and never the directories they sit in, because a read root grants everything
under it; a Claude Code that resolves a link entry to its target fails
``python_starts_with_site``.

The binary runs twice against a loopback stand-in for the Messages API, each time with a working
directory, ``HOME`` and ``CLAUDE_CONFIG_DIR`` the probe made. The stand-in answers each request
with the next fixed step as a ``Bash`` or ``Read`` tool call and reads the tool results back from
the next request. The first run's settings turn the sandbox on with no read rules. Its reads of a
file outside the roots, through Bash and through the Read tool, must succeed. The second run
differs only in the read rules, so a refusal there is theirs. A Bash read counts as refused only
when the command ran, failed, printed ``Operation not permitted`` after the path it was given,
and printed none of the file. A refusal of the command's own output names no such path, and
``/bin/cat`` meets it only after it has opened the file, so it never counts as a refused read. A
Read-tool read counts as refused when the tool result is an error that shows none of the file;
the control run, with the same tools and permissions, is what ties that refusal to the read rules
and not to a permission rule. Claude Code's sandbox confines the commands Bash runs, so the Read
tool arm is where a release that leaves the file tools unconfined shows.

What the child is given, all of it in the record:

- The flags, each taken from the ``claude --help`` of 2.1.330: ``-p``, ``--bare``,
  ``--strict-mcp-config`` with no ``--mcp-config`` (so no MCP server),
  ``--no-session-persistence``, ``--model``, ``--tools Bash,Read``, ``--allowed-tools Bash,Read``,
  ``--permission-prompts none`` and ``--settings``. Under ``--bare`` the CLI skips the hooks of
  settings and plugins, CLAUDE.md discovery and keychain reads, and authenticates only with
  ``ANTHROPIC_API_KEY``.
- The environment, by name: ``PATH``, ``HOME``, ``CLAUDE_CONFIG_DIR``, ``CLAUDE_CODE_TMPDIR``,
  ``TMPDIR`` when set, ``ANTHROPIC_BASE_URL`` (the stand-in), ``ANTHROPIC_API_KEY``
  (``PLACEHOLDER_API_KEY``, which no service accepts), the proxy variables and ``NO_PROXY``.
  Nothing else is inherited.
- The steps: fixed commands that read, list or write files the probe made, and print a marker.

The stand-in keeps each request's method and path, and the tool results it needs for the arms.
It never keeps headers. The proxy variables point at a second loopback listener that refuses every
request and keeps its method and target. ``no_other_outbound`` passes when nothing reached it,
and covers only traffic that honours the proxy variables. A child that outlives the timeout is
ended with its whole process group. In print mode the CLI ignores settings that fail validation;
such a run reads like one with no read rules, so the refusal arms fail.

The probe plants one file each under ``/tmp``, ``/var/tmp`` and ``/Users/Shared``, removed
afterwards, and reads each by the spelling a command would write. The record is written to
``LIONAGI_HOME/claude-read-probes``, keyed by the binary's resolved path, version and bytes. No
launch reads it.

Run ``python -m lionhub.providers.anthropic.claude_read_probe``.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from lionagi._errors import ConfigurationError

__all__ = (
    "PLACEHOLDER_API_KEY",
    "REQUIRED_ARMS",
    "MessagesStandIn",
    "ProxyRecorder",
    "main",
    "probe",
    "read_record_dir",
    "read_record_path",
    "run_claude",
    "write_record",
)

_RECORD_FORMAT = 1

#: Runs one claude command line, ``(argv, env, cwd)``, and returns its exit status, -1 when it
#: outlived the timeout. Injected so tests need no binary.
Runner = Callable[[list[str], dict[str, str], Path], int]

_RUN_TIMEOUT_SECONDS = 240
_EXIT_POLL_SECONDS = 0.05
_EVIDENCE_CHARS = 240
_MODEL = "probe-stand-in"
_PROMPT = "Run the probe steps."

#: The child's login. No service accepts it, and the stand-in never keeps it.
PLACEHOLDER_API_KEY = "lionagi-read-probe-placeholder"

#: Every arm a record must hold as passed for the binary to count as denying reads by default.
REQUIRED_ARMS: tuple[str, ...] = (
    "runs_under_settings",
    "control_bash_reads_outside",
    "control_read_tool_reads_outside",
    "read_inside_root_works",
    "read_cwd_works",
    "write_cwd_works",
    "read_outside_refused",
    "read_through_symlink_refused",
    "stat_outside_refused",
    "list_parent_refused",
    "read_via_tmp_link_refused",
    "read_via_var_link_refused",
    "read_users_shared_refused",
    "shell_runs_in_root",
    "bash_temp_in_granted_dir",
    "python_starts_with_site",
    "read_tool_inside_root_works",
    "read_tool_outside_refused",
    "no_other_outbound",
)

#: Directories outside every root where the probe plants a file to read, each spelled as a
#: command would write it. On macOS ``/tmp`` and ``/var`` are links to ``/private/tmp`` and
#: ``/private/var``, so a rule could match one spelling and miss the other; ``/Users/Shared``
#: lies outside both the home directory and the system set.
_PLANTED: tuple[tuple[str, str], ...] = (
    ("read_via_tmp_link_refused", "/tmp"),  # noqa: S108
    ("read_via_var_link_refused", "/var/tmp"),  # noqa: S108
    ("read_users_shared_refused", "/Users/Shared"),
)

#: What a sandbox's refusal of a read prints, from the command that was refused.
_REFUSAL = "Operation not permitted"

#: What the interpreter runs to show it starts, its site directory included.
_PYTHON_CODE = "print('python=ok')"

_PROXY_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_NO_PROXY = "127.0.0.1,localhost"
_VERSION = re.compile(r"\d+\.\d+\.\d+")


# ------------------------------------------------------------------------- loopback services
class _App(Protocol):
    def answer(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]: ...


def _sse(events: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _message(done: int, step: Mapping[str, Any] | None) -> dict[str, Any]:
    """The assistant message for a request that carries ``done`` tool results."""
    if step is None:
        content: list[dict[str, Any]] = [{"type": "text", "text": "done"}]
        stop = "end_turn"
    else:
        content = [
            {
                "type": "tool_use",
                "id": f"toolu_probe_{done}",
                "name": step["tool"],
                "input": dict(step["input"]),
            }
        ]
        stop = "tool_use"
    return {
        "id": f"msg_probe_{done}",
        "type": "message",
        "role": "assistant",
        "model": _MODEL,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _stream_events(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``message`` as the streamed events of the Messages API."""
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}
    ]
    for index, block in enumerate(message["content"]):
        if block["type"] == "tool_use":
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events += [
            {"type": "content_block_start", "index": index, "content_block": start},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    return events


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


class MessagesStandIn:
    """A loopback stand-in for the Messages API that scripts one tool call per request.

    A request that offers tools is answered with the next step as a tool call, chosen by how many
    ``tool_result`` blocks it carries; after the last step, and for a request that offers no
    tools, the answer is plain text. ``seen`` keeps each request's method and path and nothing
    else from it. ``results`` keeps each tool result's text and error flag by tool call id, read
    from the request bodies. Headers, the login among them, are never read.
    """

    def __init__(self, steps: Sequence[Mapping[str, Any]]) -> None:
        self.steps = list(steps)
        self.seen: list[str] = []
        self.results: dict[str, tuple[str, bool]] = {}
        self._lock = threading.Lock()

    def _collect(self, request: Mapping[str, Any]) -> int:
        """Keep the request's tool results; return how many it carries."""
        count = 0
        for message in request.get("messages") or []:
            if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    count += 1
                    self.results[str(block.get("tool_use_id"))] = (
                        _result_text(block.get("content")),
                        block.get("is_error") is True,
                    )
        return count

    def answer(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        route = path.split("?", 1)[0]
        with self._lock:
            self.seen.append(f"{method} {route}")
            if method != "POST":
                return 404, "application/json", b"{}"
            if route.endswith("/messages/count_tokens"):
                return 200, "application/json", b'{"input_tokens": 1}'
            if not route.endswith("/messages"):
                return 404, "application/json", b"{}"
            try:
                request = json.loads(body or b"{}")
            except ValueError:
                request = {}
            if not isinstance(request, dict):
                request = {}
            done = self._collect(request)
            step = self.steps[done] if request.get("tools") and done < len(self.steps) else None
            message = _message(done, step)
        if request.get("stream") is True:
            return 200, "text/event-stream", _sse(_stream_events(message))
        return 200, "application/json", json.dumps(message).encode()


class ProxyRecorder:
    """Where the child's proxy variables point. Every request that reaches it is refused, and
    kept as its method and target and nothing else."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def answer(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        with self._lock:
            self.seen.append(f"{method} {path}")
        return 403, "text/plain", b"refused by the read probe"


def _handler(app: _App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _any(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            status, content_type, payload = app.answer(self.command, self.path, body)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_CONNECT = _any  # noqa: N815

        def log_message(self, *args: object) -> None:
            return None

    return Handler


@contextlib.contextmanager
def _serve(app: _App) -> Iterator[str]:
    """Serve ``app`` on 127.0.0.1 for the length of the block; yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------------------------------ running claude
def run_claude(argv: list[str], env: dict[str, str], cwd: Path) -> int:
    """Run one claude command line in a process group of its own and return its exit status.

    When it outlives the timeout the whole group is killed and -1 is returned. The group is
    killed after a normal exit too, so nothing the child started outlives the run. Where the
    platform can see an exit without collecting the child, the group is killed before the child
    is collected, so its id cannot have passed to another process's group by then.
    """
    proc = subprocess.Popen(  # noqa: S603 -- the binary being probed and fixed flags
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    exited = _exited_unreaped(proc, _RUN_TIMEOUT_SECONDS)
    # The signal does not change the status of a child that has already exited.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    status = proc.wait()
    return status if exited else -1


def _exited_unreaped(proc: subprocess.Popen, timeout: float) -> bool:
    """Whether *proc* exited within *timeout*, seen without collecting it where the platform allows.

    A child that has exited but is not yet collected keeps its pid, so no other process can take
    that number as its process group id. kqueue (macOS, the BSDs) and ``os.waitid`` with
    ``WNOWAIT`` (Linux) see the exit without collecting the child. Where neither exists, or
    kqueue refuses the watch for a reason other than the process having exited, the next
    method is used; the last, ``Popen.wait``, collects the child.
    """
    kqueue = getattr(select, "kqueue", None)
    if kqueue is not None:
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
    if hasattr(os, "waitid"):
        deadline = time.monotonic() + timeout
        flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
        while True:
            try:
                if os.waitid(os.P_PID, proc.pid, flags) is not None:
                    return True
            except ChildProcessError:
                # Already collected, so it has exited.
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_EXIT_POLL_SECONDS, remaining))
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _settings(read_roots: Sequence[str] | None) -> dict[str, Any]:
    """The ``--settings`` the child gets: the sandbox on, and with ``read_roots`` the read rules."""
    sandbox: dict[str, Any] = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
    }
    if read_roots is not None:
        sandbox["filesystem"] = {"denyRead": ["/"], "allowRead": list(read_roots)}
    return {"sandbox": sandbox}


def _argv(binary: str, settings: Mapping[str, Any]) -> list[str]:
    # Each flag is in `claude --help` of 2.1.330. --tools and --allowed-tools take a list, so
    # each is followed by another flag, and the prompt follows --settings, which takes one value.
    return [
        binary,
        "-p",
        "--bare",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--model",
        _MODEL,
        "--tools",
        "Bash,Read",
        "--allowed-tools",
        "Bash,Read",
        "--permission-prompts",
        "none",
        "--settings",
        json.dumps(settings, sort_keys=True),
        _PROMPT,
    ]


def _env(
    base_url: str, proxy_url: str, home: Path, config: Path, claude_tmp: Path
) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(config),
        "CLAUDE_CODE_TMPDIR": str(claude_tmp),
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": PLACEHOLDER_API_KEY,
        "NO_PROXY": _NO_PROXY,
        "no_proxy": _NO_PROXY,
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    for name in _PROXY_NAMES:
        env[name] = proxy_url
    return env


def _bash(cmd: str) -> dict[str, Any]:
    return {"tool": "Bash", "input": {"command": f"{cmd}; echo rc=$?", "description": "probe"}}


def _read(path: Path) -> dict[str, Any]:
    return {"tool": "Read", "input": {"file_path": str(path)}}


def _run(
    binary: str,
    runner: Runner,
    *,
    steps: Sequence[tuple[str, Mapping[str, Any]]],
    read_roots: Sequence[str] | None,
    home: Path,
    config: Path,
    cwd: Path,
    claude_tmp: Path,
) -> tuple[dict[str, tuple[str, bool] | None], dict[str, Any]]:
    """One claude run: step name to the tool result the child sent back (None when it never
    did), and what the run was given and what reached the two listeners."""
    stand_in = MessagesStandIn([step for _, step in steps])
    proxy = ProxyRecorder()
    settings = _settings(read_roots)
    argv = _argv(binary, settings)
    with _serve(stand_in) as base_url, _serve(proxy) as proxy_url:
        env = _env(base_url, proxy_url, home, config, claude_tmp)
        status = runner(argv, env, cwd)
    results = {name: stand_in.results.get(f"toolu_probe_{i}") for i, (name, _) in enumerate(steps)}
    run = {
        "argv": argv,
        "env_names": sorted(env),
        "claude_tmp": str(claude_tmp),
        "exit_status": status,
        "requests": list(stand_in.seen),
        "proxied": list(proxy.seen),
    }
    return results, run


# ------------------------------------------------------------------------------- arm verdicts
def _rc(text: str | None) -> int | None:
    """The exit status a step's own ``echo rc=$?`` printed, or None when it printed none."""
    text = text or ""
    last = re.search(r"rc=(\d+)\s*\Z", text)
    if last:
        return int(last.group(1))
    found = re.findall(r"\brc=(\d+)", text)
    return int(found[-1]) if found else None


def _text(result: tuple[str, bool] | None) -> str:
    return result[0] if result is not None else ""


def _bash_ok(result: tuple[str, bool] | None, marker: str | None = None) -> bool:
    text = _text(result)
    return result is not None and _rc(text) == 0 and (marker is None or marker in text)


def _bash_refused(result: tuple[str, bool] | None, path: str, marker: str | None = None) -> bool:
    """The command ran and failed with the sandbox's refusal of ``path``, as the command was
    given it, and printed no ``marker``. ``cat: stdout: Operation not permitted`` is a refusal of
    the command's own output, which ``/bin/cat`` meets only after opening the file."""
    text = _text(result)
    return (
        result is not None
        and _rc(text) not in (None, 0, 127)
        and f"{path}: {_REFUSAL}" in text
        and (marker is None or marker not in text)
    )


def _read_ok(result: tuple[str, bool] | None, marker: str) -> bool:
    return result is not None and not result[1] and marker in result[0]


def _read_refused(result: tuple[str, bool] | None, marker: str) -> bool:
    return result is not None and result[1] and marker not in result[0]


def _evidence(*results: tuple[str, bool] | None) -> str:
    parts = []
    for result in results:
        if result is None:
            parts.append("no tool result")
            continue
        clipped = " ".join(result[0].split())[:_EVIDENCE_CHARS]
        parts.append(f"{'error' if result[1] else 'ok'} rc={_rc(result[0])} {clipped}")
    return " | ".join(parts)


def _arm(passed: bool, *results: tuple[str, bool] | None, note: str = "") -> dict[str, Any]:
    evidence = _evidence(*results) + (f" ({note})" if note else "")
    return {"passed": bool(passed), "evidence": evidence}


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _system_read_roots() -> tuple[str, ...]:
    from lionhub.tools._seatbelt import _system_read_roots as system_read_roots

    return system_read_roots()


def _child_python() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def _claude_temp(override: Path) -> Path:
    """Where Claude Code keeps its internal temp files when ``CLAUDE_CODE_TMPDIR`` is
    ``override``: it appends ``claude-<uid>`` on Unix."""
    return override / f"claude-{os.getuid()}"


def _python_links(python: str, read_roots: Sequence[str]) -> tuple[str, ...]:
    """Each link on the way to ``python`` that lies outside every read root, as written.

    The interpreter reads these to find its prefix, and the prefixes in the system set resolve
    only the last of them. A directory on the way is never returned: a read root grants
    everything under it.
    """
    from lionhub.tools._seatbelt import _lookup_paths

    links = (
        path
        for path in _lookup_paths(python)
        if os.path.islink(path) and not any(_within(Path(path), Path(r)) for r in read_roots)
    )
    return tuple(dict.fromkeys(links))


def _reported_tmpdir(result: tuple[str, bool] | None) -> str | None:
    """The ``TMPDIR`` the ``tmpdir`` step printed, or None when the step did not run."""
    text = _text(result)
    found = re.search(r"^tmpdir=(.+)$", text, re.MULTILINE)
    if result is None or _rc(text) != 0 or found is None:
        return None
    return found.group(1).strip()


def _probe(
    binary: str, runner: Runner, root: Path
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...], dict[str, Any]]:
    nonce = uuid.uuid4().hex[:12]
    marks = {n: f"{n.upper()}-{nonce}" for n in ("inside", "cwd", "outside", "planted")}
    cwd, readroot, outside_dir = root / "cwd", root / "readroot", root / "outside"
    homes = {n: root / f"home-{n}" for n in ("control", "roots")}
    configs = {n: root / f"config-{n}" for n in ("control", "roots")}
    # Short names: Claude Code is documented to give Bash the system temp directory instead of
    # an override path it finds too long.
    temps = {n: root / f"t{n[0]}" for n in ("control", "roots")}
    for path in (cwd, readroot, outside_dir, *homes.values(), *configs.values(), *temps.values()):
        path.mkdir(parents=True)
    inside, cwd_file = readroot / "inside.txt", cwd / "cwd.txt"
    outside, alias = outside_dir / "target.txt", readroot / "outside-alias"
    inside.write_text(marks["inside"] + "\n")
    cwd_file.write_text(marks["cwd"] + "\n")
    outside.write_text(marks["outside"] + "\n")
    alias.symlink_to(outside)

    python = _child_python()
    granted_temp = _claude_temp(temps["roots"])
    read_roots = (str(readroot), str(cwd), *_system_read_roots())
    read_roots = (*read_roots, *_python_links(python, read_roots), str(granted_temp))
    for path in (root, outside_dir):
        for readable in read_roots:
            if _within(path, Path(readable)) and readable != str(cwd):
                raise ConfigurationError(
                    f"the probe's path {str(path)!r} lies inside the read root {readable!r}; "
                    "give a --scratch directory outside it"
                )

    q = shlex.quote
    control, control_run = _run(
        binary,
        runner,
        steps=[("outside", _bash(f"/bin/cat {q(str(outside))}")), ("read_tool", _read(outside))],
        read_roots=None,
        home=homes["control"],
        config=configs["control"],
        cwd=cwd,
        claude_tmp=temps["control"],
    )

    arms: dict[str, dict[str, Any]] = {}
    planted: list[tuple[str, Path]] = []
    try:
        for name, directory in _PLANTED:
            try:
                fd, made = tempfile.mkstemp(prefix="lionagi-read-probe-", dir=directory)
            except OSError as exc:
                arms[name] = {
                    "passed": False,
                    "evidence": f"no file could be planted in {directory}: {exc}",
                }
                continue
            os.write(fd, (marks["planted"] + "\n").encode())
            os.close(fd)
            planted.append((name, Path(made)))
            real = Path(os.path.realpath(made))
            for readable in read_roots:
                if _within(real, Path(readable)):
                    raise ConfigurationError(
                        f"the probe's file {made!r} lies inside the read root {readable!r}, so "
                        "no refusal can be measured there"
                    )
        steps = [
            ("inside", _bash(f"/bin/cat {q(str(inside))}")),
            ("cwd_read", _bash(f"/bin/cat {q(str(cwd_file))}")),
            ("cwd_write", _bash(f"echo w > {q(str(cwd / 'w'))}")),
            ("outside", _bash(f"/bin/cat {q(str(outside))}")),
            # The sandbox judges the path a link resolves to, and a confined command can make the
            # link.
            ("symlink", _bash(f"/bin/cat {q(str(alias))}")),
            ("stat", _bash(f"/bin/ls -d {q(str(outside))}")),
            # The directory above the working directory and the read root.
            ("list_parent", _bash(f"/bin/ls {q(str(root))}")),
            *((name, _bash(f"/bin/cat {q(str(made))}")) for name, made in planted),
            ("shell", _bash("/bin/ls")),
            ("tmpdir", _bash('printf "tmpdir=%s\\n" "$TMPDIR"')),
            ("python", _bash(f"{q(python)} -I -c {q(_PYTHON_CODE)}")),
            ("read_tool_inside", _read(inside)),
            ("read_tool_outside", _read(outside)),
        ]
        run, roots_run = _run(
            binary,
            runner,
            steps=steps,
            read_roots=read_roots,
            home=homes["roots"],
            config=configs["roots"],
            cwd=cwd,
            claude_tmp=temps["roots"],
        )
    finally:
        for _, made in planted:
            made.unlink(missing_ok=True)

    answered = sum(1 for result in run.values() if result is not None)
    arms["runs_under_settings"] = {
        "passed": answered == len(run),
        "evidence": (
            f"{answered} of {len(run)} steps of the run with the read rules came back, exit "
            f"status {roots_run['exit_status']}; none means claude did not run under the settings"
        ),
    }
    arms["control_bash_reads_outside"] = _arm(
        _bash_ok(control["outside"], marks["outside"]), control["outside"]
    )
    control_read = _read_ok(control["read_tool"], marks["outside"])
    arms["control_read_tool_reads_outside"] = _arm(control_read, control["read_tool"])
    arms["read_inside_root_works"] = _arm(_bash_ok(run["inside"], marks["inside"]), run["inside"])
    arms["read_cwd_works"] = _arm(_bash_ok(run["cwd_read"], marks["cwd"]), run["cwd_read"])
    arms["write_cwd_works"] = _arm(
        _bash_ok(run["cwd_write"]) and (cwd / "w").is_file(), run["cwd_write"]
    )
    arms["read_outside_refused"] = _arm(
        _bash_refused(run["outside"], str(outside), marks["outside"]), run["outside"]
    )
    arms["read_through_symlink_refused"] = _arm(
        _bash_refused(run["symlink"], str(alias), marks["outside"]), run["symlink"]
    )
    arms["stat_outside_refused"] = _arm(_bash_refused(run["stat"], str(outside)), run["stat"])
    arms["list_parent_refused"] = _arm(
        _bash_refused(run["list_parent"], str(root), readroot.name), run["list_parent"]
    )
    for name, made in planted:
        arms[name] = _arm(_bash_refused(run[name], str(made), marks["planted"]), run[name])
    arms["shell_runs_in_root"] = _arm(
        _bash_ok(run["shell"]) and cwd_file.name in _text(run["shell"]).split(), run["shell"]
    )
    reported = _reported_tmpdir(run["tmpdir"])
    arms["bash_temp_in_granted_dir"] = _arm(
        reported is not None and _within(Path(os.path.realpath(reported)), granted_temp),
        run["tmpdir"],
        note=f"granted {granted_temp}",
    )
    arms["python_starts_with_site"] = _arm(_bash_ok(run["python"], "python=ok"), run["python"])
    arms["read_tool_inside_root_works"] = _arm(
        _read_ok(run["read_tool_inside"], marks["inside"]), run["read_tool_inside"]
    )
    arms["read_tool_outside_refused"] = _arm(
        control_read and _read_refused(run["read_tool_outside"], marks["outside"]),
        run["read_tool_outside"],
        note=(
            "the control run read the file with the same tools and permissions"
            if control_read
            else "the control run did not read the file, so a refusal here is not the read rules'"
        ),
    )
    proxied = [*control_run["proxied"], *roots_run["proxied"]]
    arms["no_other_outbound"] = {
        "passed": not proxied,
        "evidence": (
            f"{len(proxied)} requests reached the listener the proxy variables name"
            + (f": {', '.join(proxied)[:_EVIDENCE_CHARS]}" if proxied else "")
            + "; this covers only traffic that honours the proxy variables"
        ),
    }
    if outside.read_text() != marks["outside"] + "\n":
        raise ConfigurationError(f"the probe's outside file {str(outside)!r} changed")
    runs = {
        "control": {**control_run, "settings": _settings(None)},
        "roots": {**roots_run, "settings": _settings(read_roots)},
    }
    return {name: arms[name] for name in REQUIRED_ARMS}, read_roots, runs


def _temp_roots() -> list[Path]:
    """``/tmp``, the platform's temp directory and ``$TMPDIR``, each as written and resolved."""
    named = ["/tmp", "/var/tmp", tempfile.gettempdir()]  # noqa: S108
    if os.environ.get("TMPDIR"):
        named.append(os.environ["TMPDIR"])
    roots: list[Path] = []
    for name in named:
        for spelling in (os.path.abspath(name), os.path.realpath(name)):
            if Path(spelling) not in roots:
                roots.append(Path(spelling))
    return roots


def _scratch_parent(scratch: Path | str | None) -> Path:
    """The directory the probe makes its root in, created when absent: a caller-given path, else
    ``<lionagi home>/state/claude-read-probe``. A path inside a temp directory is refused: the
    sandbox may leave temp directories readable whatever the read rules say."""
    from lionagi import _paths

    chosen = (
        scratch
        if scratch is not None
        else Path(_paths.LIONAGI_HOME) / "state" / "claude-read-probe"
    )
    parent = Path(os.path.realpath(os.path.expanduser(chosen)))
    for temp in _temp_roots():
        if _within(parent, temp):
            raise ConfigurationError(
                f"the scratch root {str(parent)!r} is inside the temp directory {str(temp)!r}; "
                "give a directory outside /tmp and $TMPDIR with --scratch"
            )
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def probe(
    binary: str,
    *,
    runner: Runner | None = None,
    scratch: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...], dict[str, Any]]:
    """Run every arm against ``binary``: arm name to ``{"passed": bool, "evidence": str}``, the
    read roots measured, and what each of the two runs was given and saw.

    ``runner`` defaults to ``run_claude``. ``scratch`` is a directory to make the probe's root
    in; the root is a fresh subdirectory, removed afterwards.
    """
    parent = _scratch_parent(scratch)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        return _probe(binary, runner or run_claude, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------------- the record
def read_record_dir() -> Path:
    """Where read records live: ``LIONAGI_HOME/claude-read-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "claude-read-probes"


def read_record_path(resolved: str, version: str, sha256: str) -> Path:
    """The record for one binary, named by its resolved path, version and the SHA-256 of its
    bytes: a binary replaced in place under the same path and version has no record."""
    digest = hashlib.sha256(f"{resolved}\0{version}\0{sha256}".encode()).hexdigest()[:24]
    return read_record_dir() / f"claude-{version}-{digest}.json"


def _binary_version(binary: str) -> str | None:
    """The first ``N.N.N`` ``binary --version`` prints, or None when it cannot be read."""
    try:
        proc = subprocess.run(  # noqa: S603 -- the binary being probed and a literal flag
            [binary, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    found = _VERSION.search(proc.stdout)
    return found.group(0) if found else None


def _sha256(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def write_record(
    binary: str,
    arms: Mapping[str, Mapping[str, Any]],
    read_roots: Sequence[str],
    runs: Mapping[str, Any],
) -> Path:
    """Write the read record for ``binary``, replacing any earlier one."""
    resolved = os.path.realpath(binary)
    version = _binary_version(binary)
    if version is None:
        raise RuntimeError(f"the version of {binary!r} cannot be read")
    sha256 = _sha256(resolved)
    if sha256 is None:
        raise RuntimeError(f"the bytes of {binary!r} cannot be read")
    path = read_record_path(resolved, version, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "binary": resolved,
        "version": version,
        "sha256": sha256,
        "read_roots": list(read_roots),
        "runs": {name: dict(run) for name, run in runs.items()},
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "arms": {name: dict(arm) for name, arm in arms.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the claude binary on PATH and write its read record; 0 only when every arm passed."""
    parser = argparse.ArgumentParser(prog="python -m lionhub.providers.anthropic.claude_read_probe")
    parser.add_argument("--binary", help="claude binary to probe (default: claude on PATH)")
    parser.add_argument(
        "--scratch",
        help=(
            "directory to make the probe's root in (default: "
            "<lionagi home>/state/claude-read-probe); never inside /tmp or $TMPDIR"
        ),
    )
    args = parser.parse_args(argv)
    binary = args.binary or shutil.which("claude")
    if not binary:
        print("claude is not on PATH", file=sys.stderr)
        return 2
    try:
        arms, read_roots, runs = probe(binary, scratch=Path(args.scratch) if args.scratch else None)
        path = write_record(binary, arms, read_roots, runs)
    except (ConfigurationError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for name, arm in arms.items():
        print(f"{'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"read roots: {', '.join(read_roots)}")
    print(f"record: {path}")
    return 0 if all(arm["passed"] for arm in arms.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
