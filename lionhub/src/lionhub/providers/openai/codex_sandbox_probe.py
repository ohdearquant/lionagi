# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Probe one codex binary for the permission profile lionagi runs codex children under.

Codex reports none of what its permission profiles enforce: its startup line prints a legacy
sandbox mode whatever the profile does. So a profiled launch runs only on a binary that has a
passing record, and this module writes it::

    python -m lionhub.providers.openai.codex_sandbox_probe

It builds the profile with the same ``permission_profile_table`` and ``deny_entries`` a launch
uses, runs the codex binary on PATH six times against a loopback stand-in for a model
provider, in a scratch working directory and a scratch ``CODEX_HOME``, and has the model ask the
child to read, write and fetch. Acceptance is what the tool outputs the stand-in receives show
(a denied canary that was not read, a write that did not land), never codex's own report. A
denied real path is opened for zero bytes and its exit status recorded; it is never read out.
The record holds every arm in ``REQUIRED_ARMS`` with its verdict and evidence, whether it
passed or not, and is keyed by the binary's resolved path and version.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from lionhub.providers.openai._codex_permissions import (
    _RECORD_FORMAT,
    REQUIRED_ARMS,
    binary_sha256,
    codex_binary_version,
    deny_entries,
    permission_profile_table,
    probe_record_path,
)

__all__ = (
    "ScratchRootError",
    "StubProvider",
    "UnixListener",
    "main",
    "probe",
    "run_codex",
    "tool_outputs",
    "write_record",
)

#: Runs one codex command line: ``(argv, env, cwd)``. Injected so tests need no binary.
Runner = Callable[[list[str], dict[str, str], Path], object]

_RUN_TIMEOUT_SECONDS = 180
_EVIDENCE_CHARS = 240
_REAL_PATHS_OPENED = 5
# The shell opens its argument for reading and reads nothing: ``:`` is a no-op, so the exit
# status is the status of the redirection's open and nothing else.
_OPEN_FOR_ZERO_BYTES = ': < "$1"'
# The connect a child (and the probe itself, outside the sandbox) runs against one unix socket.
# It prints one token and exits: ``connect=ok``, ``connect=<errno name>`` from its own errno
# table, ``connect=timeout``, or ``connect=<exception name>`` for an OSError with no errno. The
# verdicts read the token, never the traceback text or the errno's number. Given a second
# argument it sends that as a mark once connected: the probe's own connects do, the child's never.
_CONNECT_CODE = (
    "import errno, socket, sys\n"
    "s = socket.socket(socket.AF_UNIX)\n"
    "s.settimeout(3)\n"
    "try:\n"
    "    s.connect(sys.argv[1])\n"
    "    if len(sys.argv) > 2:\n"
    "        s.sendall(sys.argv[2].encode())\n"
    "except socket.timeout:\n"
    "    print('connect=timeout')\n"
    "    sys.exit(1)\n"
    "except OSError as e:\n"
    "    print('connect=' + (errno.errorcode.get(e.errno or -1) or type(e).__name__))\n"
    "    sys.exit(1)\n"
    "print('connect=ok')\n"
)
_CONNECT_TOKEN = re.compile(r"^connect=([A-Za-z0-9_]+)[ \t]*$", re.MULTILINE)
_OUTSIDE_TIMEOUT_SECONDS = 10
_MARK_PREFIX = "lionagi-probe-mark-"
_MARK_READ_SECONDS = 3


# ------------------------------------------------------------------------- loopback provider
def _sse(events: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class StubProvider:
    """A loopback stand-in for a Responses API model.

    Each request is answered with the next step as an ``exec_command`` call, chosen by how many
    ``function_call_output`` items the request carries; after the last step it answers with a
    plain message. Every request body is kept in ``requests``. ``GET /ping`` answers ``pong``,
    the target of the network arms. It listens on 127.0.0.1 only.
    """

    def __init__(self, steps: Sequence[Mapping[str, Any]]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _reply(self, request: Mapping[str, Any]) -> bytes:
        items = request.get("input") or []
        done = sum(
            1 for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"
        )
        rid = f"resp_{done}"
        if done < len(self.steps):
            item: dict[str, Any] = {
                "type": "function_call",
                "id": f"fc_{done}",
                "call_id": f"call_{done}",
                "name": "exec_command",
                "arguments": json.dumps(self.steps[done]),
                "status": "completed",
            }
        else:
            item = {
                "type": "message",
                "id": "msg_final",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "done", "annotations": []}],
            }
        usage = {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        }
        return _sse(
            [
                {"type": "response.created", "response": {"id": rid}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": rid, "usage": usage}},
            ]
        )

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 -- http.server's method name
                body = b"pong" if self.path == "/ping" else b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 -- http.server's method name
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    request = json.loads(raw)
                except ValueError:
                    request = {}
                if not isinstance(request, dict):
                    request = {}
                provider.requests.append(request)
                body = provider._reply(request)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return None

        return Handler


def tool_outputs(requests: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """``call_id`` to the tool output text codex sent back, over every request received."""
    found: dict[str, str] = {}
    for request in requests:
        for item in request.get("input") or []:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                output = item.get("output")
                found[str(item.get("call_id"))] = (
                    output if isinstance(output, str) else json.dumps(output)
                )
    return found


# ------------------------------------------------------------------- scratch root and sockets
class ScratchRootError(ValueError):
    """The probe's scratch root would sit where the profile under test leaves writes open."""


class UnixListener:
    """A listener on one unix socket, counting the connections that reach it.

    The probe binds it outside the sandbox, at a path inside or outside the denied set, and has
    the child try to connect. ``accepted`` counts connections in the order the kernel queued
    them. A connection whose first bytes are a mark (the probe's own connects send one) is
    recorded under that mark with its place in that order, and ``wait_for_mark`` waits for it:
    once a mark sent after the child exited is recorded, every connection queued before it has
    been counted. ``stop`` also takes the connections still queued.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.accepted = 0
        self._marks: dict[str, int] = {}
        self._counted = threading.Condition()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        """Bind and listen; raises OSError when the path cannot be bound (too long, refused)."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.path))
            sock.listen(8)
            sock.settimeout(0.2)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        sock = self._sock
        while sock is not None and not self._stopping.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._counted:
                self.accepted += 1
                place = self.accepted
            threading.Thread(target=self._read_mark, args=(conn, place), daemon=True).start()

    def _read_mark(self, conn: socket.socket, place: int) -> None:
        """Record the connection at ``place`` under the mark it sent, if it sent one."""
        try:
            conn.settimeout(_MARK_READ_SECONDS)
            data = conn.recv(256)
        except OSError:
            data = b""
        finally:
            conn.close()
        mark = data.decode("ascii", "replace").strip()
        if mark.startswith(_MARK_PREFIX):
            with self._counted:
                self._marks.setdefault(mark, place)
                self._counted.notify_all()

    def wait_for_mark(self, mark: str, timeout: float) -> int | None:
        """The place of the connection that sent ``mark`` in the order the listener counted
        connections, once it is recorded; None when it is not recorded within ``timeout``."""
        with self._counted:
            self._counted.wait_for(lambda: mark in self._marks, timeout)
            return self._marks.get(mark)

    def stop(self) -> None:
        """Stop serving, count what is still queued, close and remove the socket."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            self._sock.setblocking(False)
            while True:
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    break
                with self._counted:
                    self.accepted += 1
                conn.close()
            self._sock.close()
            self._sock = None
        self.path.unlink(missing_ok=True)


def _start_listener(path: Path) -> tuple[UnixListener | None, str | None]:
    """A started listener at ``path``, or ``(None, why)`` when it cannot be bound."""
    listener = UnixListener(path)
    try:
        listener.start()
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return listener, None


def _connect_token(text: str | None) -> str | None:
    """The last token the connect code printed in ``text``, or None when it printed none."""
    found = _CONNECT_TOKEN.findall(text or "")
    return found[-1] if found else None


def _outside_connect(path: Path, mark: str) -> str:
    """The token the connect code prints when the probe runs it itself, outside the sandbox,
    sending ``mark`` once connected."""
    try:
        done = subprocess.run(  # noqa: S603 -- this interpreter and the probe's own code
            [_child_python(), "-c", _CONNECT_CODE, str(path), mark],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_OUTSIDE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not-run-{type(exc).__name__}"
    return _connect_token(done.stdout) or "no-token"


def _listening(
    denied: Path, control: Path, run: Callable[[], dict[str, str | None]]
) -> tuple[dict[str, str | None], dict[str, Any]]:
    """``run()`` with a listener bound here, outside the sandbox, at each socket path: the run's
    outputs, and how many connections reached each listener from the run alone.

    The run is bracketed by an outside control: before and after it, the probe runs the same
    connect code itself, outside the sandbox, to both sockets, each connect sending a mark of its
    own. A listener counts connections in the order they were queued, so once the after-run
    mark is recorded every connection queued before it has been counted, the child's included.
    The run's own count of a listener is the after-run mark's place less the before-run mark's
    place less 1, read at no earlier moment. When an outside connect did not print
    ``connect=ok`` or its mark was not recorded, the count is None and every socket verdict on
    the run fails.
    """
    denied_listener, denied_error = _start_listener(denied)
    control_listener, control_error = _start_listener(control)
    up = denied_listener is not None and control_listener is not None
    listeners = {"denied": (denied, denied_listener), "control": (control, control_listener)}
    run_id = uuid.uuid4().hex
    outside: dict[str, dict[str, str]] = {}
    places: dict[str, dict[str, int | None]] = {}

    def bracket(when: str) -> None:
        mark = f"{_MARK_PREFIX}{run_id}-{when}"
        outside[when], places[when] = {}, {}
        for key, (path, listener) in listeners.items():
            printed = _outside_connect(path, mark)
            outside[when][key] = printed
            places[when][key] = (
                listener.wait_for_mark(mark, _OUTSIDE_TIMEOUT_SECONDS)
                if printed == "ok" and listener is not None
                else None
            )

    try:
        if up:
            bracket("before")
        outputs = run()
        if up:
            bracket("after")
    finally:
        for _path, listener in listeners.values():
            if listener is not None:
                listener.stop()

    def own(key: str) -> int | None:
        before = places.get("before", {}).get(key)
        after = places.get("after", {}).get(key)
        if before is None or after is None:
            return None
        return after - before - 1

    seen = {
        "up": up,
        "denied": own("denied"),
        "control": own("control"),
        "error": denied_error or control_error,
        "outside": outside,
        "outside_reached": up and own("denied") is not None and own("control") is not None,
    }
    return outputs, seen


def _counts_note(outputs: Mapping[str, str | None], seen: Mapping[str, Any]) -> str:
    return (
        f"the denied-path socket accepted {seen['denied']} connection(s), "
        f"the control socket {seen['control']}; the child printed "
        f"connect={_connect_token(outputs.get('unix_denied'))} for the denied socket and "
        f"connect={_connect_token(outputs.get('unix_control'))} for the control"
    )


def _not_reached_note(outputs: Mapping[str, str | None], seen: Mapping[str, Any]) -> str:
    return (
        f"the probe's own connects outside the sandbox did not reach both listeners before and "
        f"after the run ({seen['outside']}), so no count is read; the child printed "
        f"connect={_connect_token(outputs.get('unix_denied'))} for the denied socket and "
        f"connect={_connect_token(outputs.get('unix_control'))} for the control; fails closed"
    )


def _connects(outputs: Mapping[str, str | None], seen: Mapping[str, Any]) -> dict[str, Any]:
    """What the record keeps of one run's connects: each token seen and each in-run count."""
    return {
        "child": {
            "denied": _connect_token(outputs.get("unix_denied")),
            "control": _connect_token(outputs.get("unix_control")),
        },
        "outside": seen.get("outside") or {},
        "in_run": {"denied": seen.get("denied"), "control": seen.get("control")},
    }


def _network_on_verdict(
    outputs: Mapping[str, str | None], seen: Mapping[str, Any]
) -> tuple[bool, str]:
    """Whether the network-on run's child was refused the denied socket by the sandbox (EPERM)
    and reached the control, by its own tokens and the listeners' counts; and a note."""
    if not seen["up"]:
        return False, f"a listener could not be bound ({seen['error']}); fails closed"
    if not seen["outside_reached"]:
        return False, _not_reached_note(outputs, seen)
    passed = bool(
        _connect_token(outputs.get("unix_denied")) == "EPERM"
        and seen["denied"] == 0
        and _ok(outputs.get("unix_control"))
        and _connect_token(outputs.get("unix_control")) == "ok"
        and (seen["control"] or 0) >= 1
    )
    return passed, _counts_note(outputs, seen)


#: How a passing network-off run's control connect ended, as the record names it.
CONTROL_REACHED = "control reached"
CONTROL_REFUSED = "control refused"


def _network_off_verdict(
    outputs: Mapping[str, str | None], seen: Mapping[str, Any]
) -> tuple[bool, str | None, str]:
    """Whether one network-off run passes the socket check, which case it showed
    (``CONTROL_REACHED`` or ``CONTROL_REFUSED``, None when it failed), and a note.

    It passes when the probe's own connects reached both listeners before and after the run,
    the child's denied connect printed connect=EPERM with the denied listener counting 0, and
    the control either printed connect=ok with its listener counting at least 1, or printed
    connect=EPERM with its listener counting 0. Every other outcome fails.
    """
    if not seen["up"]:
        return False, None, f"a listener could not be bound ({seen['error']}); fails closed"
    if not seen["outside_reached"]:
        return False, None, _not_reached_note(outputs, seen)
    note = _counts_note(outputs, seen)
    if _connect_token(outputs.get("unix_denied")) != "EPERM" or seen["denied"] != 0:
        return False, None, f"{note}; the denied connect was not refused by the sandbox"
    control = _connect_token(outputs.get("unix_control"))
    if control == "ok" and _ok(outputs.get("unix_control")) and (seen["control"] or 0) >= 1:
        return (
            True,
            CONTROL_REACHED,
            f"{note}; {CONTROL_REACHED}: the refusal is scoped to the denied path",
        )
    if control == "EPERM" and seen["control"] == 0:
        return True, CONTROL_REFUSED, f"{note}; {CONTROL_REFUSED}: no local socket connects at all"
    return (
        False,
        None,
        f"{note}; the control connect neither reached its listener nor was refused by the sandbox",
    )


def _child_python() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def _connect_command(python: str, path: Path) -> str:
    """The shell command that connects to the unix socket at ``path`` and exits."""
    return f"{shlex.quote(python)} -c {shlex.quote(_CONNECT_CODE)} {shlex.quote(str(path))}"


def _temp_roots() -> list[Path]:
    """The temporary directories a child under ``:workspace`` can write to: ``/tmp``, the
    platform's temp directory and ``$TMPDIR``, each as written and as resolved."""
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
    """The directory the probe makes its working directory in, created when absent.

    A caller-given path, else ``<lionagi home>/state/codex-probe``. It is refused when it lies
    inside a temp root: a child under ``:workspace`` can write there whatever the profile
    table says, so a write-entry arm would pass without the entry being granted.
    """
    from lionagi import _paths

    chosen = scratch if scratch is not None else Path(_paths.LIONAGI_HOME) / "state" / "codex-probe"
    parent = Path(os.path.realpath(os.path.expanduser(chosen)))
    for temp in _temp_roots():
        if parent == temp or temp in parent.parents:
            raise ScratchRootError(
                f"the scratch root {str(parent)!r} is inside the temp directory {str(temp)!r}, "
                "which a child under the workspace profile can write to; give a directory "
                "outside /tmp and $TMPDIR with --scratch"
            )
    parent.mkdir(parents=True, exist_ok=True)
    return parent


# ------------------------------------------------------------------------------ running codex
def run_codex(argv: list[str], env: dict[str, str], cwd: Path) -> int:
    """Run one codex command line; -1 when it outlives the timeout."""
    try:
        return subprocess.run(  # noqa: S603 -- the binary being probed and fixed flags
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        return -1


def _argv(
    binary: str,
    url: str,
    table: Mapping[str, Any],
    cwd: Path,
    add_dirs: Sequence[Path],
    scratch: str | None = None,
):
    from lionhub.providers.openai.codex import (
        _permission_profile_overrides,
        _scratch_env_overrides,
    )

    provider = f'model_providers.stub={{name="stub",base_url="{url}/v1",wire_api="responses"}}'
    argv = [
        binary,
        "exec",
        "--skip-git-repo-check",
        # The flags a contained launch passes, since the probe certifies a contained launch.
        # Plugins off is also the one that stops codex cloning its plugin catalog into its home
        # at startup, an outbound connection the probe would otherwise make itself.
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "-c",
        'model_provider="stub"',
        "-c",
        'model="stub-model"',
        "-c",
        provider,
    ]
    for path in add_dirs:
        argv += ["--add-dir", str(path)]
    argv += _permission_profile_overrides(table)
    if scratch is not None:
        # The same keys, in the same place, as a launch with a scratch directory passes them.
        argv += _scratch_env_overrides(scratch)
    argv += ["-C", str(cwd), "probe"]
    return argv


def _env(home: Path) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    env["CODEX_HOME"] = str(home)
    return env


def _step(cmd: str) -> dict[str, Any]:
    return {"cmd": f"{cmd}; echo rc=$?", "login": False, "shell": "/bin/sh"}


def _run(
    binary: str,
    runner: Runner,
    *,
    commands: Sequence[tuple[str, str]],
    table: Mapping[str, Any],
    home: Path,
    cwd: Path,
    add_dirs: Sequence[Path] = (),
    scratch: str | None = None,
) -> dict[str, str | None]:
    """One codex run: step name to the tool output the child sent back (None when it never did).
    ``scratch`` sets TMPDIR, TMP and TEMP for the child's commands as a launch does."""
    provider = StubProvider([_step(cmd) for _, cmd in commands])
    provider.start()
    try:
        runner(_argv(binary, provider.base_url, table, cwd, add_dirs, scratch), _env(home), cwd)
    finally:
        provider.stop()
    outputs = tool_outputs(provider.requests)
    return {name: outputs.get(f"call_{i}") for i, (name, _) in enumerate(commands)}


# ------------------------------------------------------------------------------- arm verdicts
def _rc(text: str | None) -> int | None:
    """The exit status a step's own ``echo rc=$?`` printed, or None when it printed none.

    The marker starts its own line only when the command's output ended in a newline: ``curl
    -s`` prints a body without one, so that step's output reads ``pongrc=0``. A marker that ends
    the text is therefore read wherever it starts; otherwise the last marker that starts a word
    is.
    """
    text = text or ""
    last = re.search(r"rc=(\d+)\s*\Z", text)
    if last:
        return int(last.group(1))
    found = re.findall(r"\brc=(\d+)", text)
    return int(found[-1]) if found else None


def _refused(text: str | None, marker: str | None = None) -> bool:
    """The step ran and failed (not a missing tool, not silence) and leaked no marker."""
    rc = _rc(text)
    return rc not in (None, 0, 127) and (marker is None or marker not in (text or ""))


def _ok(text: str | None, marker: str | None = None) -> bool:
    return _rc(text) == 0 and (marker is None or marker in (text or ""))


def _evidence(*texts: str | None) -> str:
    parts = []
    for text in texts:
        clipped = " ".join((text or "no output").split())[:_EVIDENCE_CHARS]
        parts.append(f"rc={_rc(text)} {clipped}")
    return " | ".join(parts)


def _arm(passed: bool, *texts: str | None, note: str = "") -> dict[str, Any]:
    evidence = _evidence(*texts) + (f" ({note})" if note else "")
    return {"passed": bool(passed), "evidence": evidence}


_SCRATCH_VAR = re.compile(r"^(TMPDIR|TMP|TEMP)=(.*)$", re.MULTILINE)


def _scratch_env_verdict(
    outputs: Mapping[str, str | None], scratch: Path, written: Path
) -> tuple[bool, str]:
    """Whether the scratch run's child saw TMPDIR, TMP and TEMP each equal to ``scratch`` and
    the file it wrote under ``$TMPDIR`` landed at ``written``; and a note saying what it saw."""
    from lionhub.providers.openai.codex import _SCRATCH_ENV_NAMES

    seen = dict(_SCRATCH_VAR.findall(outputs.get("scratch_vars") or ""))
    equal = _ok(outputs.get("scratch_vars")) and all(
        seen.get(name) == str(scratch) for name in _SCRATCH_ENV_NAMES
    )
    landed = written.is_file()
    shown = ", ".join(f"{name}={seen.get(name)!r}" for name in _SCRATCH_ENV_NAMES)
    note = (
        f"the child saw {shown}; the scratch directory is {str(scratch)!r}; the file written "
        f"under $TMPDIR {'landed' if landed else 'did not land'} in it"
    )
    return bool(equal and _ok(outputs.get("scratch_write")) and landed), note


def _probe(binary: str, runner: Runner, root: Path) -> dict[str, dict[str, Any]]:
    from lionhub.agent.child_sandbox import resolve_child_sandbox

    nonce = uuid.uuid4().hex[:12]
    deny_mark, open_mark = f"DENY-CANARY-{nonce}", f"OPEN-CANARY-{nonce}"
    dirs = {
        n: root / n for n in ("cwd", "entry", "outside", "deny", "open", "scratch", "envscratch")
    }
    homes = {
        n: root / f"home-{n}"
        for n in ("heavy", "light", "user", "readonly", "rowrite", "scratchenv")
    }
    for path in (*dirs.values(), *homes.values()):
        path.mkdir(parents=True)
    (dirs["deny"] / "secret.txt").write_text(deny_mark + "\n")
    (dirs["open"] / "f.txt").write_text(open_mark + "\n")
    (dirs["open"] / "ro.txt").write_text(open_mark + "\n")
    (dirs["open"] / "rw.txt").write_text(open_mark + "\n")
    for name, home in homes.items():
        (home / "auth.json").write_text(f'{{"key": "AUTH-CANARY-{name}-{nonce}"}}\n')
    (homes["user"] / "config.toml").write_text('sandbox_mode = "workspace-write"\n')

    policy_deny = list(resolve_child_sandbox(None).deny_read)
    deny_read = [str(dirs["deny"]), *policy_deny]

    def table(
        home: Path,
        *,
        light: bool,
        read_only: bool = False,
        read_only_write: Sequence[str] = (),
        extra_write: Sequence[str] = (),
    ) -> dict[str, Any]:
        return permission_profile_table(
            # Expanded against the HOME the probe's child gets, as a launch expands its entries
            # against the child's HOME.
            deny=deny_entries(deny_read, home, _env(home)),
            write=([] if light else [str(dirs["entry"])]) + list(extra_write),
            light=light,
            read_only=read_only,
            # The heavy runs set the network on, so the network arms test the profile's
            # network key both ways: off refuses, on reaches the listener. A read-only table has
            # the network off, as a read-only launch's does.
            network=not light and not read_only,
            read_only_write=read_only_write,
        )

    q = shlex.quote
    secret, opened = q(str(dirs["deny"] / "secret.txt")), q(str(dirs["open"] / "f.txt"))
    open_zero = f"/bin/sh -c {q(_OPEN_FOR_ZERO_BYTES)} _"
    python = _child_python()
    denied_sock, control_sock = dirs["deny"] / "d.sock", dirs["open"] / "c.sock"
    real = [
        p
        for p in deny_entries(policy_deny, homes["heavy"], _env(homes["heavy"]))
        if os.path.isfile(p) and not p.startswith(str(root))
    ][:_REAL_PATHS_OPENED]
    heavy_commands = [
        ("deny_canary", f"/bin/cat {secret}"),
        ("own_codex_home", f"/bin/cat {q(str(homes['heavy'] / 'auth.json'))}"),
        ("read_control", f"/bin/cat {opened}"),
        ("write_cwd", f"echo w > {q(str(dirs['cwd'] / 'w'))}"),
        ("write_entry", f"echo w > {q(str(dirs['entry'] / 'w'))}"),
        ("write_outside", f"echo w > {q(str(dirs['outside'] / 'w'))}"),
        # Each denied real path is opened for zero bytes by the shell. The control opens a file
        # the profile leaves readable, the same way in the same run, so a shell that cannot open
        # anything is told apart from a path that was refused.
        ("open_control", f"{open_zero} {opened}"),
        *((f"real_{i}", f"{open_zero} {q(p)}") for i, p in enumerate(real)),
        ("unix_control", _connect_command(python, control_sock)),
        ("unix_denied", _connect_command(python, denied_sock)),
    ]
    # The light arms aim at /tmp and $TMPDIR on purpose: the profile must refuse a write to both.
    # With TMPDIR unset there is no second directory, so that arm is skipped, not run on /tmp
    # again.
    tmp_slash = Path("/tmp") / f"lionagi-probe-{nonce}"  # noqa: S108
    tmpdir = os.environ.get("TMPDIR")
    tmp_env = Path(tmpdir) / f"lionagi-probe-{nonce}-env" if tmpdir else None
    # The two connects run in each network-off run too: the light run and the read-only run.
    connects = [
        ("unix_control", _connect_command(python, control_sock)),
        ("unix_denied", _connect_command(python, denied_sock)),
    ]
    light_commands = [("tmp_slash", f"echo w > {q(str(tmp_slash))}")]
    if tmp_env is not None:
        light_commands.append(("tmp_env", f"echo w > {q(str(tmp_env))}"))
    light_commands += connects
    user_commands = [
        ("deny_canary", f"/bin/cat {secret}"),
        ("own_codex_home", f"/bin/cat {q(str(homes['user'] / 'auth.json'))}"),
        ("read_control", f"/bin/cat {opened}"),
        ("write_outside", f"echo w > {q(str(dirs['outside'] / 'w2'))}"),
    ]
    # The read-only run starts from codex's `:read-only` built-in, as a read-only launch does: a
    # write into its own working directory must be refused while a read in the same run works.
    ro_read, ro_written = dirs["open"] / "ro.txt", dirs["cwd"] / "ro"
    readonly_commands = [
        ("read_control", f"/bin/cat {q(str(ro_read))}"),
        ("write_cwd", f"echo w > {q(str(ro_written))}"),
        *connects,
    ]
    # A `:read-only` table that grants one write entry, as a codex scratch directory needs: the
    # entry write must land, a write into the working directory must not, a read must work.
    rw_entry, rw_cwd = dirs["scratch"] / "w", dirs["cwd"] / "rw"
    rowrite_commands = [
        ("write_entry", f"echo w > {q(str(rw_entry))}"),
        ("write_cwd", f"echo w > {q(str(rw_cwd))}"),
        ("read_control", f"/bin/cat {q(str(dirs['open'] / 'rw.txt'))}"),
    ]
    # A light table granting one scratch directory, with TMPDIR, TMP and TEMP set to it the way
    # a launch with a scratch directory sets them, while the child's own environment carries the
    # probe's TMPDIR: the commands must see all three equal to the scratch directory, and a file
    # written under $TMPDIR must land in it.
    env_scratch = dirs["envscratch"]
    env_written = env_scratch / f"scratch-env-{nonce}"
    scratchenv_commands = [
        ("scratch_vars", 'echo "TMPDIR=$TMPDIR"; echo "TMP=$TMP"; echo "TEMP=$TEMP"'),
        ("scratch_write", f'echo w > "$TMPDIR"/{q(env_written.name)}'),
    ]

    # The network step targets each run's own stub, whose port is known only once it listens,
    # so ``_run_with_network`` appends it to the plan after the stub starts.
    # Two unix sockets, bound here outside the sandbox for each run that connects: one inside the
    # denied directory, one in a directory no denied path covers. The child connects to each. The
    # write entry is granted by the profile table alone: no run passes --add-dir.
    heavy, heavy_seen = _listening(
        denied_sock,
        control_sock,
        lambda: _run_with_network(
            binary,
            runner,
            heavy_commands,
            table(homes["heavy"], light=False),
            homes["heavy"],
            dirs["cwd"],
            [],
        ),
    )
    light, light_seen = _listening(
        denied_sock,
        control_sock,
        lambda: _run_with_network(
            binary,
            runner,
            light_commands,
            table(homes["light"], light=True),
            homes["light"],
            dirs["cwd"],
            [],
        ),
    )
    user = _run(
        binary,
        runner,
        commands=user_commands,
        table=table(homes["user"], light=False),
        home=homes["user"],
        cwd=dirs["cwd"],
    )
    readonly, readonly_seen = _listening(
        denied_sock,
        control_sock,
        lambda: _run(
            binary,
            runner,
            commands=readonly_commands,
            table=table(homes["readonly"], light=False, read_only=True),
            home=homes["readonly"],
            cwd=dirs["cwd"],
        ),
    )
    rowrite = _run(
        binary,
        runner,
        commands=rowrite_commands,
        table=table(
            homes["rowrite"], light=False, read_only=True, read_only_write=[str(dirs["scratch"])]
        ),
        home=homes["rowrite"],
        cwd=dirs["cwd"],
    )
    scratchenv = _run(
        binary,
        runner,
        commands=scratchenv_commands,
        table=table(homes["scratchenv"], light=True, extra_write=[str(env_scratch)]),
        home=homes["scratchenv"],
        cwd=dirs["cwd"],
        scratch=str(env_scratch),
    )
    scratch_env_ok, scratch_env_note = _scratch_env_verdict(scratchenv, env_scratch, env_written)

    outside_gone = not (dirs["outside"] / "w").exists()
    # No real denied path opened is no evidence: the arm fails closed rather than pass on an
    # empty list.
    real_refused = bool(real) and all(_refused(heavy.get(f"real_{i}")) for i in range(len(real)))
    control_open_ok = _ok(heavy["open_control"])
    slash_landed = tmp_slash.exists()
    env_landed = tmp_env is not None and tmp_env.exists()
    for path in (tmp_slash, tmp_env):
        if path is not None:
            path.unlink(missing_ok=True)
    unix_on_ok, unix_on_note = _network_on_verdict(heavy, heavy_seen)
    light_unix_ok, light_case, light_unix_note = _network_off_verdict(light, light_seen)
    readonly_unix_ok, readonly_case, readonly_unix_note = _network_off_verdict(
        readonly, readonly_seen
    )

    arms = {
        "deny_canary": _arm(
            _refused(heavy["deny_canary"], deny_mark) and control_open_ok and real_refused,
            heavy["deny_canary"],
            heavy["open_control"],
            note=(
                f"control open of a readable file succeeded: {control_open_ok}; "
                f"{len(real)} real denied path(s) opened for 0 bytes, all refused: {real_refused}"
            ),
        ),
        "unix_socket_refused_network_on": {
            **_arm(unix_on_ok, heavy["unix_denied"], heavy["unix_control"], note=unix_on_note),
            "connects": {"heavy": _connects(heavy, heavy_seen)},
        },
        # Both network-off runs must pass: a read-only launch rests on the read-only run's own
        # measurement, a light or network-off heavy one on the light run's. Each run's case
        # (control reached or refused) and each connect's token and count are recorded.
        "unix_socket_refused_network_off": {
            **_arm(
                light_unix_ok and readonly_unix_ok,
                light["unix_denied"],
                light["unix_control"],
                readonly["unix_denied"],
                readonly["unix_control"],
                note=f"light run: {light_unix_note}; read-only run: {readonly_unix_note}",
            ),
            "cases": {"light": light_case, "read_only": readonly_case},
            "connects": {
                "light": _connects(light, light_seen),
                "read_only": _connects(readonly, readonly_seen),
            },
        },
        "own_codex_home": _arm(
            _refused(heavy["own_codex_home"], f"AUTH-CANARY-heavy-{nonce}"), heavy["own_codex_home"]
        ),
        "read_control": _arm(_ok(heavy["read_control"], open_mark), heavy["read_control"]),
        "write_cwd": _arm(
            _ok(heavy["write_cwd"]) and (dirs["cwd"] / "w").exists(), heavy["write_cwd"]
        ),
        "write_entry": _arm(
            _ok(heavy["write_entry"]) and (dirs["entry"] / "w").exists(),
            heavy["write_entry"],
            note="the entry is granted by the profile table alone, with no --add-dir",
        ),
        "write_outside_refused": _arm(
            _refused(heavy["write_outside"]) and outside_gone, heavy["write_outside"]
        ),
        "light_tmp_refused": _arm(
            _refused(light["tmp_slash"]) and not slash_landed, light["tmp_slash"]
        ),
        "light_tmpdir_refused": (
            _arm(_refused(light["tmp_env"]) and not env_landed, light["tmp_env"])
            if tmp_env is not None
            else {
                "passed": False,
                "skipped": True,
                "evidence": (
                    "skipped: TMPDIR is unset in the probe's environment, so there is no second "
                    "temporary directory to test; /tmp is covered by light_tmp_refused"
                ),
            }
        ),
        "light_network_off": _arm(_refused(light["network"], "pong"), light["network"]),
        "heavy_network_on": _arm(_ok(heavy["network"], "pong"), heavy["network"]),
        "user_layer_sandbox_mode": _arm(
            _ok(user["read_control"], open_mark)
            and _refused(user["deny_canary"], deny_mark)
            and _refused(user["own_codex_home"], f"AUTH-CANARY-user-{nonce}")
            and _refused(user["write_outside"])
            and not (dirs["outside"] / "w2").exists(),
            user["deny_canary"],
            user["own_codex_home"],
            user["write_outside"],
        ),
        "read_only_cwd_write_refused": _arm(
            _ok(readonly["read_control"], open_mark)
            and _refused(readonly["write_cwd"])
            and not ro_written.exists(),
            readonly["read_control"],
            readonly["write_cwd"],
            note="a :read-only table; the read of a readable file is the same run's control",
        ),
        "read_only_write_root": _arm(
            _ok(rowrite["write_entry"])
            and rw_entry.exists()
            and _refused(rowrite["write_cwd"])
            and not rw_cwd.exists()
            and _ok(rowrite["read_control"], open_mark),
            rowrite["write_entry"],
            rowrite["write_cwd"],
            rowrite["read_control"],
            note="a :read-only table granting one write entry, as a read-only launch with a "
            "scratch directory renders it",
        ),
        "scratch_env": _arm(
            scratch_env_ok,
            scratchenv["scratch_vars"],
            scratchenv["scratch_write"],
            note=scratch_env_note,
        ),
    }
    return {name: arms[name] for name in REQUIRED_ARMS}


def _run_with_network(binary, runner, commands, table, home, cwd, add_dirs):
    """``_run`` with a final ``network`` step aimed at the run's own loopback stub."""
    names = [n for n, _ in commands] + ["network"]
    provider = StubProvider([])
    provider.start()
    try:
        ping = f"curl -s -m 3 {provider.base_url}/ping"
        provider.steps = [_step(cmd) for _, cmd in (*commands, ("network", ping))]
        runner(_argv(binary, provider.base_url, table, cwd, add_dirs), _env(home), cwd)
    finally:
        provider.stop()
    outputs = tool_outputs(provider.requests)
    return {name: outputs.get(f"call_{i}") for i, name in enumerate(names)}


def probe(
    binary: str, *, runner: Runner = run_codex, scratch: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Run every arm against ``binary``; arm name to ``{"passed": bool, "evidence": str}``.

    ``scratch`` is a directory to make the probe's working directory in; it defaults to
    ``<lionagi home>/state/codex-probe`` and is refused inside ``/tmp`` or ``$TMPDIR``. The
    working directory is a fresh subdirectory, removed afterwards. An arm that cannot apply on
    this host is recorded as ``{"passed": False, "skipped": True, ...}``.
    """
    parent = _scratch_parent(scratch)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        return _probe(binary, runner, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_record(binary: str, arms: Mapping[str, Mapping[str, Any]]) -> Path:
    """Write the record ``probe_record`` reads for ``binary``, replacing any earlier one."""
    resolved = os.path.realpath(binary)
    version = codex_binary_version(binary)
    if version is None:
        raise RuntimeError(f"the version of {binary!r} cannot be read")
    sha256 = binary_sha256(resolved)
    if sha256 is None:
        raise RuntimeError(f"the bytes of {binary!r} cannot be read")
    path = probe_record_path(resolved, version, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "binary": resolved,
        "version": version,
        "sha256": sha256,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "arms": {name: dict(arm) for name, arm in arms.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the codex binary on PATH and write its record; 0 only when every arm passed."""
    parser = argparse.ArgumentParser(prog="python -m lionhub.providers.openai.codex_sandbox_probe")
    parser.add_argument("--binary", help="codex binary to probe (default: codex on PATH)")
    parser.add_argument(
        "--scratch",
        help=(
            "directory to make the probe's working directory in (default: "
            "<lionagi home>/state/codex-probe); never inside /tmp or $TMPDIR"
        ),
    )
    args = parser.parse_args(argv)
    binary = args.binary or shutil.which("codex")
    if not binary:
        print("codex is not on PATH", file=sys.stderr)
        return 2
    try:
        arms = probe(binary, scratch=Path(args.scratch)) if args.scratch else probe(binary)
    except ScratchRootError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path = write_record(binary, arms)
    failed = [name for name, arm in arms.items() if not arm["passed"] and not arm.get("skipped")]
    for name, arm in arms.items():
        outcome = "skip" if arm.get("skipped") else "pass" if arm["passed"] else "FAIL"
        print(f"{outcome}  {name}  {arm['evidence']}")
    print(f"record: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
