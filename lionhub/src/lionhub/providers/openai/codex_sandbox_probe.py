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
    PERMISSION_PROFILE,
    REQUIRED_ARMS,
    codex_binary_version,
    deny_entries,
    permission_profile_table,
    probe_record_path,
)

__all__ = ("StubProvider", "main", "probe", "run_codex", "tool_outputs", "write_record")

#: Runs one codex command line: ``(argv, env, cwd)``. Injected so tests need no binary.
Runner = Callable[[list[str], dict[str, str], Path], object]

_RUN_TIMEOUT_SECONDS = 180
_EVIDENCE_CHARS = 240
_REAL_PATHS_OPENED = 5


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
    from lionhub.providers.openai.codex import _rendered_override_value

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
    argv += [
        "-c",
        f"default_permissions={_rendered_override_value(PERMISSION_PROFILE, 'The profile name')}",
        "-c",
        f"permissions.{PERMISSION_PROFILE}={_rendered_override_value(table, 'The profile')}",
        "-C",
        str(cwd),
        "probe",
    ]
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
    found = re.findall(r"\brc=(\d+)", text or "")
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
    homes = {n: root / f"home-{n}" for n in ("heavy", "light", "user")}
    for path in (*dirs.values(), *homes.values()):
        path.mkdir(parents=True)
    (dirs["deny"] / "secret.txt").write_text(deny_mark + "\n")
    (dirs["open"] / "f.txt").write_text(open_mark + "\n")
    for name, home in homes.items():
        (home / "auth.json").write_text(f'{{"key": "AUTH-CANARY-{name}-{nonce}"}}\n')
    (homes["user"] / "config.toml").write_text('sandbox_mode = "workspace-write"\n')

    policy_deny = list(resolve_child_sandbox(None).deny_read)
    deny_read = [str(dirs["deny"]), *policy_deny]

    def table(home: Path, *, light: bool) -> dict[str, Any]:
        return permission_profile_table(
            deny=deny_entries(deny_read, home),
            write=[] if light else [str(dirs["entry"])],
            light=light,
            read_only=False,
        )

    q = shlex.quote
    secret, opened = q(str(dirs["deny"] / "secret.txt")), q(str(dirs["open"] / "f.txt"))
    real = [
        p
        for p in deny_entries(policy_deny, homes["heavy"])
        if os.path.isfile(p) and not p.startswith(str(root))
    ][:_REAL_PATHS_OPENED]
    heavy_commands = [
        ("deny_canary", f"/bin/cat {secret}"),
        ("own_codex_home", f"/bin/cat {q(str(homes['heavy'] / 'auth.json'))}"),
        ("read_control", f"/bin/cat {opened}"),
        ("write_cwd", f"echo w > {q(str(dirs['cwd'] / 'w'))}"),
        ("write_entry", f"echo w > {q(str(dirs['entry'] / 'w'))}"),
        ("write_outside", f"echo w > {q(str(dirs['outside'] / 'w'))}"),
        *((f"real_{i}", f"head -c 0 {q(p)}") for i, p in enumerate(real)),
    ]
    # The light arms aim at /tmp and $TMPDIR on purpose: the profile must refuse a write to both.
    tmp_slash = Path("/tmp") / f"lionagi-probe-{nonce}"  # noqa: S108
    tmp_env = Path(os.environ.get("TMPDIR") or "/tmp") / f"lionagi-probe-{nonce}-env"  # noqa: S108
    light_commands = [
        ("tmp_slash", f"echo w > {q(str(tmp_slash))}"),
        ("tmp_env", f"echo w > {q(str(tmp_env))}"),
    ]
    user_commands = [
        ("deny_canary", f"/bin/cat {secret}"),
        ("own_codex_home", f"/bin/cat {q(str(homes['user'] / 'auth.json'))}"),
        ("read_control", f"/bin/cat {opened}"),
        ("write_outside", f"echo w > {q(str(dirs['outside'] / 'w2'))}"),
    ]

    # The network step targets each run's own stub, whose port is known only once it listens,
    # so ``_run_with_network`` appends it to the plan after the stub starts.
    heavy = _run_with_network(
        binary,
        runner,
        heavy_commands,
        table(homes["heavy"], light=False),
        homes["heavy"],
        dirs["cwd"],
        [dirs["entry"]],
    )
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
        add_dirs=[dirs["entry"]],
    )

    outside_gone = not (dirs["outside"] / "w").exists()
    real_refused = all(_refused(heavy.get(f"real_{i}")) for i in range(len(real)))
    tmp_landed = tmp_slash.exists() or tmp_env.exists()
    for path in (tmp_slash, tmp_env):
        path.unlink(missing_ok=True)

    arms = {
        "deny_canary": _arm(
            _refused(heavy["deny_canary"], deny_mark) and real_refused,
            heavy["deny_canary"],
            note=f"{len(real)} real denied path(s) opened for 0 bytes, all refused: {real_refused}",
        ),
        "own_codex_home": _arm(
            _refused(heavy["own_codex_home"], f"AUTH-CANARY-heavy-{nonce}"), heavy["own_codex_home"]
        ),
        "read_control": _arm(_ok(heavy["read_control"], open_mark), heavy["read_control"]),
        "write_cwd": _arm(
            _ok(heavy["write_cwd"]) and (dirs["cwd"] / "w").exists(), heavy["write_cwd"]
        ),
        "write_entry": _arm(
            _ok(heavy["write_entry"]) and (dirs["entry"] / "w").exists(), heavy["write_entry"]
        ),
        "write_outside_refused": _arm(
            _refused(heavy["write_outside"]) and outside_gone, heavy["write_outside"]
        ),
        "light_tmp_refused": _arm(
            _refused(light["tmp_slash"]) and _refused(light["tmp_env"]) and not tmp_landed,
            light["tmp_slash"],
            light["tmp_env"],
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
    """Run every arm against ``binary``; arm name to ``{"passed": bool, "evidence": str}``."""
    root = (
        Path(scratch)
        if scratch
        else Path(os.path.realpath(tempfile.mkdtemp(prefix="codex-probe-")))
    )
    try:
        return _probe(binary, runner, root)
    finally:
        if scratch is None:
            shutil.rmtree(root, ignore_errors=True)


def write_record(binary: str, arms: Mapping[str, Mapping[str, Any]]) -> Path:
    """Write the record ``probe_record`` reads for ``binary``, replacing any earlier one."""
    resolved = os.path.realpath(binary)
    version = codex_binary_version(binary)
    if version is None:
        raise RuntimeError(f"the version of {binary!r} cannot be read")
    path = probe_record_path(resolved, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "binary": resolved,
        "version": version,
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
    args = parser.parse_args(argv)
    binary = args.binary or shutil.which("codex")
    if not binary:
        print("codex is not on PATH", file=sys.stderr)
        return 2
    arms = probe(binary)
    path = write_record(binary, arms)
    failed = [name for name, arm in arms.items() if not arm["passed"]]
    for name, arm in arms.items():
        print(f"{'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"record: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
