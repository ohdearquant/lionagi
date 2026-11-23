# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Probe one codex binary for the permission profile lionagi runs codex children under.

Codex reports none of what its permission profiles enforce: its startup line prints a legacy
sandbox mode whatever the profile does. So a profiled launch runs only on a binary that has a
passing record, and this module writes it::

    python -m lionhub.providers.openai.codex_sandbox_probe

It builds the profile with the same ``permission_profile_table`` and ``deny_entries`` a launch
uses, runs the codex binary on PATH three times against a loopback stand-in for a model
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
_CONNECT_ONE_LINER = (
    "import socket, sys; s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(sys.argv[1])"
)


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
    the child try to connect. ``accepted`` is exact once ``stop`` has returned: ``stop`` takes the
    connections the kernel queued that the serving thread had not yet accepted.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.accepted = 0
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
            self.accepted += 1
            conn.close()

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


def _child_python() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def _connect_command(python: str, path: Path) -> str:
    """The shell command that connects to the unix socket at ``path`` and exits."""
    return f"{shlex.quote(python)} -c {shlex.quote(_CONNECT_ONE_LINER)} {shlex.quote(str(path))}"


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


def _argv(binary: str, url: str, table: Mapping[str, Any], cwd: Path, add_dirs: Sequence[Path]):
    from lionhub.providers.openai.codex import _permission_profile_overrides

    provider = f'model_providers.stub={{name="stub",base_url="{url}/v1",wire_api="responses"}}'
    argv = [
        binary,
        "exec",
        "--skip-git-repo-check",
        "-c",
        'model_provider="stub"',
        "-c",
        'model="stub-model"',
        "-c",
        provider,
    ]
    for path in add_dirs:
        argv += ["--add-dir", str(path)]
    argv += [*_permission_profile_overrides(table), "-C", str(cwd), "probe"]
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
) -> dict[str, str | None]:
    """One codex run: step name to the tool output the child sent back (None when it never did)."""
    provider = StubProvider([_step(cmd) for _, cmd in commands])
    provider.start()
    try:
        runner(_argv(binary, provider.base_url, table, cwd, add_dirs), _env(home), cwd)
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


def _probe(binary: str, runner: Runner, root: Path) -> dict[str, dict[str, Any]]:
    from lionhub.agent.child_sandbox import resolve_child_sandbox

    nonce = uuid.uuid4().hex[:12]
    deny_mark, open_mark = f"DENY-CANARY-{nonce}", f"OPEN-CANARY-{nonce}"
    dirs = {n: root / n for n in ("cwd", "entry", "outside", "deny", "open")}
    homes = {n: root / f"home-{n}" for n in ("heavy", "light", "user", "readonly")}
    for path in (*dirs.values(), *homes.values()):
        path.mkdir(parents=True)
    (dirs["deny"] / "secret.txt").write_text(deny_mark + "\n")
    (dirs["open"] / "f.txt").write_text(open_mark + "\n")
    (dirs["open"] / "ro.txt").write_text(open_mark + "\n")
    for name, home in homes.items():
        (home / "auth.json").write_text(f'{{"key": "AUTH-CANARY-{name}-{nonce}"}}\n')
    (homes["user"] / "config.toml").write_text('sandbox_mode = "workspace-write"\n')

    policy_deny = list(resolve_child_sandbox(None).deny_read)
    deny_read = [str(dirs["deny"]), *policy_deny]

    def table(home: Path, *, light: bool, read_only: bool = False) -> dict[str, Any]:
        return permission_profile_table(
            # Expanded against the HOME the probe's child gets, as a launch expands its entries
            # against the child's HOME.
            deny=deny_entries(deny_read, home, _env(home)),
            write=[] if light else [str(dirs["entry"])],
            light=light,
            read_only=read_only,
            # The heavy runs set the network on, so the network arms test the profile's
            # network key both ways: off refuses, on reaches the listener. A read-only table has
            # the network off, as a read-only launch's does.
            network=not light and not read_only,
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
    light_commands = [("tmp_slash", f"echo w > {q(str(tmp_slash))}")]
    if tmp_env is not None:
        light_commands.append(("tmp_env", f"echo w > {q(str(tmp_env))}"))
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
    ]

    # The network step targets each run's own stub, whose port is known only once it listens,
    # so ``_run_with_network`` appends it to the plan after the stub starts.
    # Two unix sockets, bound here outside the sandbox: one inside the denied directory, one in a
    # directory no denied path covers. The child connects to each. The write entry is granted by
    # the profile table alone: no run passes --add-dir.
    denied_listener, denied_error = _start_listener(denied_sock)
    control_listener, control_error = _start_listener(control_sock)
    try:
        heavy = _run_with_network(
            binary,
            runner,
            heavy_commands,
            table(homes["heavy"], light=False),
            homes["heavy"],
            dirs["cwd"],
            [],
        )
    finally:
        for listener in (denied_listener, control_listener):
            if listener is not None:
                listener.stop()
    light = _run_with_network(
        binary,
        runner,
        light_commands,
        table(homes["light"], light=True),
        homes["light"],
        dirs["cwd"],
        [],
    )
    user = _run(
        binary,
        runner,
        commands=user_commands,
        table=table(homes["user"], light=False),
        home=homes["user"],
        cwd=dirs["cwd"],
    )
    readonly = _run(
        binary,
        runner,
        commands=readonly_commands,
        table=table(homes["readonly"], light=False, read_only=True),
        home=homes["readonly"],
        cwd=dirs["cwd"],
    )

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
    listeners_up = denied_listener is not None and control_listener is not None
    denied_hits = denied_listener.accepted if denied_listener is not None else None
    control_hits = control_listener.accepted if control_listener is not None else None
    unix_ok = (
        listeners_up
        and _refused(heavy["unix_denied"])
        and denied_hits == 0
        and _ok(heavy["unix_control"])
        and bool(control_hits)
    )
    if listeners_up:
        unix_note = (
            f"the denied-path socket accepted {denied_hits} connection(s), "
            f"the control socket {control_hits}"
        )
    else:
        unix_note = f"a listener could not be bound ({denied_error or control_error}); fails closed"

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
        "unix_socket_refused": _arm(
            unix_ok, heavy["unix_denied"], heavy["unix_control"], note=unix_note
        ),
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
