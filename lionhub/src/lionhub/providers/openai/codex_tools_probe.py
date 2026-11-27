# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Which codex settings, if any, leave its model only one MCP server's tools, for one binary.

ADR-0041 Amendment 9 (D2) proposes that a codex leg run on a computer host only once some set of
codex settings is shown to leave the model no tool but those of the one MCP server the host names.
The proof is the ``tools`` array in the Responses requests codex sends, never codex's own report
of its tools. This probe measures candidate settings against that array and writes the table.

A candidate is a string of codex arguments, given with ``--candidate`` and split as a POSIX shell
splits it; the caller takes candidates from the installed CLI's own configuration reference. Every
candidate is one run and one row. The probe writes the stdio stand-in MCP server of
:mod:`lionhub.providers._tools_probe_server` and runs the binary against a loopback stand-in for
the Responses API, served by the claude read probe's listener, which keeps the label of every tool
each request offers (its ``name``, else its ``type``; a tool nested in another is labelled
``outer/inner``). The stand-in answers the first request that offers the server's ``ping`` tool
with one call of it, named as the Responses API names a call (a tool nested in a namespace tool by
the namespace and its own name), and every other request with a plain message. Each run has its
own working
directory and ``CODEX_HOME``, and two decoy servers are named where codex finds ambient
configuration: one in ``CODEX_HOME/config.toml``, one in a ``.codex/config.toml`` in the working
directory. The runs, each with the flags of a contained launch (apps and plugins off, the stand-in
as the model provider):

- ``native_control``: the launch's MCP flags, :func:`mcp_flags`, and no candidate;
- ``ambient_control``: the same without ``--ignore-user-config``;
- one per candidate: the launch's MCP flags followed by the candidate's arguments.

The arms of each candidate's row:

- ``offers_only_server_tools``: the run sent at least one request offering the server's tool, and
  no request, the first or any later one, offered any other tool.
- ``control_offers_native_tools``: ``native_control`` offered a tool of codex's own, so a
  candidate that offers none is not a blind read of the array. The server's tool need not be
  offered in that run: with its own tools on, a binary may hold MCP tools back. The evidence says
  whether it was offered. The arm is the same for every row.
- ``ambient_servers_absent``: ``ambient_control`` offered the user-scope decoy's tool or asked
  that decoy server for its tools, which shows this binary loads that decoy, and the candidate's
  run offered neither decoy's tool and started neither decoy server. The evidence names each
  decoy the control loaded; the project decoy's absence shows something only when it is one.
- ``server_connected``: the server received ``initialize``, ``tools/list`` and ``tools/call``, and
  the call came back with its answer.

The record is written to ``LIONAGI_HOME/codex-tools-probes``, keyed by the binary's resolved path,
version and bytes, with every row, the controls and the MCP flags measured. No launch reads it.

Run ``python -m lionhub.providers.openai.codex_tools_probe --candidate '<args>' ...``. It exits 0
when some candidate passes every arm, 1 when none does, 3 when ``native_control`` offered no tool
of codex's own (the table then shows nothing), and 2 when it cannot run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lionhub.providers._tools_probe_server import ANSWER, MCP_SERVER_SOURCE, TOOL, logged_methods
from lionhub.providers.anthropic import claude_read_probe as read_probe
from lionhub.providers.openai import codex_sandbox_probe as sandbox_probe
from lionhub.providers.openai._codex_permissions import binary_sha256, codex_binary_version
from lionhub.providers.openai.codex import _toml_string

__all__ = (
    "REQUIRED_ARMS",
    "SERVER_NAME",
    "ToolsStandIn",
    "main",
    "mcp_flags",
    "parse_candidate",
    "probe",
    "tool_labels",
    "tools_record_dir",
    "tools_record_path",
    "write_record",
)

_RECORD_FORMAT = 1

#: Runs one codex command line, ``(argv, env, cwd)``, and returns its exit status, -1 when it
#: outlived the timeout. Injected so tests need no binary.
Runner = Callable[[list[str], dict[str, str], Path], int]

#: The name the probe gives its one MCP server.
SERVER_NAME = "probe"
_USER_DECOY = "decoyuser"
_PROJECT_DECOY = "decoyproject"
_SERVERS = (SERVER_NAME, _USER_DECOY, _PROJECT_DECOY)
_PROMPT = "Call the ping tool once, then answer done."
_EVIDENCE_CHARS = 240

#: Every arm a candidate's row must hold as passed for its settings to count as leaving the model
#: only the one server's tools.
REQUIRED_ARMS: tuple[str, ...] = (
    "offers_only_server_tools",
    "control_offers_native_tools",
    "ambient_servers_absent",
    "server_connected",
)

# Arguments a candidate may not carry: the probe sets the working directory, and a second
# subcommand or prompt would change what is measured.
_REFUSED_TOKENS = frozenset({"-C", "--cd", "exec", "resume"})


def parse_candidate(text: str) -> list[str]:
    """The codex arguments one ``--candidate`` names, split as a POSIX shell splits them."""
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"the candidate {text!r} cannot be split: {exc}") from exc
    if not tokens:
        raise ValueError("a candidate names at least one codex argument")
    refused = sorted(
        {token.split("=", 1)[0] for token in tokens} & _REFUSED_TOKENS,
    )
    if refused:
        raise ValueError(
            f"the candidate {text!r} carries {', '.join(refused)}, which the probe sets itself"
        )
    return tokens


def _toml_array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def mcp_flags(server: str, command: str, args: Sequence[str]) -> list[str]:
    """The flags that give a codex child ``server`` as its one MCP server: the user config
    dropped, so no server it names loads, ``server`` defined on the command line, and that
    server's tools approved. The approval is scoped to ``server`` alone: under an approval policy
    of ``never``, codex 0.162.1 refuses every MCP call whose tool is not approved in the config,
    and the launch does not know the server's tools ahead of time."""
    return [
        "--ignore-user-config",
        "-c",
        f"mcp_servers.{server}.command={_toml_string(command)}",
        "-c",
        f"mcp_servers.{server}.args={_toml_array(args)}",
        "-c",
        f"mcp_servers.{server}.default_tools_approval_mode={_toml_string('approve')}",
    ]


def tool_labels(tools: object) -> list[str]:
    """The sorted labels of the tools a Responses request offers: each tool's ``name``, else its
    ``type``, else ``unnamed``; a tool that holds a list of tools is labelled by each of them as
    ``outer/inner``. Nothing else of a tool is kept."""
    labels: list[str] = []

    def walk(items: object, prefix: str) -> None:
        if not isinstance(items, list):
            return
        for tool in items:
            if not isinstance(tool, dict):
                continue
            name, kind = tool.get("name"), tool.get("type")
            if isinstance(name, str) and name:
                label = name
            elif isinstance(kind, str) and kind:
                label = kind
            else:
                label = "unnamed"
            full = f"{prefix}/{label}" if prefix else label
            nested = tool.get("tools")
            if isinstance(nested, list) and nested:
                walk(nested, full)
            else:
                labels.append(full)

    walk(tools, "")
    return sorted(labels)


def _owner(label: str) -> str | None:
    """Which of the probe's servers a tool label belongs to: one that ends in the server's tool
    and names the server as a word of its own. None for every other tool."""
    if not label.endswith(TOOL):
        return None
    for server in _SERVERS:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(server)}(?![A-Za-z0-9])", label):
            return server
    return None


class ToolsStandIn:
    """A loopback stand-in for the Responses API that keeps the tool labels of each request.

    A request that offers the probe server's tool and carries no tool output yet is answered with
    one call of that tool, every other request with a plain message. The call names the tool as
    the Responses API does: a tool nested in a namespace tool by its own ``name`` and the
    namespace's name as ``namespace``, any other tool by its label alone. ``offered`` keeps each request's labels in order, ``outputs`` the tool output text
    codex sent back by call id, and ``routes`` each request's method and path. Nothing else of a
    request is kept.
    """

    def __init__(self) -> None:
        self.offered: list[list[str]] = []
        self.outputs: dict[str, str] = {}
        self.routes: list[str] = []
        self._lock = threading.Lock()

    def answer(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        route = path.split("?", 1)[0]
        with self._lock:
            self.routes.append(f"{method} {route}")
            if method != "POST":
                return 200, "application/json", b"{}"
            try:
                request = json.loads(body or b"{}")
            except ValueError:
                request = {}
            if not isinstance(request, dict):
                request = {}
            labels = tool_labels(request.get("tools"))
            self.offered.append(labels)
            done = 0
            for item in request.get("input") or []:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    done += 1
                    output = item.get("output")
                    self.outputs[str(item.get("call_id"))] = (
                        output if isinstance(output, str) else json.dumps(output)
                    )
            target = next((label for label in labels if _owner(label) == SERVER_NAME), None)
        rid = f"resp_{len(self.offered)}"
        if done == 0 and target is not None:
            namespace, _, name = target.rpartition("/")
            item_out: dict[str, Any] = {
                "type": "function_call",
                "id": "fc_0",
                "call_id": "call_0",
                "name": name,
                "arguments": "{}",
                "status": "completed",
            }
            if namespace:
                item_out["namespace"] = namespace
        else:
            item_out = {
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
        events = [
            {"type": "response.created", "response": {"id": rid}},
            {"type": "response.output_item.added", "output_index": 0, "item": item_out},
            {"type": "response.output_item.done", "output_index": 0, "item": item_out},
            {"type": "response.completed", "response": {"id": rid, "usage": usage}},
        ]
        return 200, "text/event-stream", read_probe._sse(events)


def _argv(binary: str, url: str, flags: Sequence[str], cwd: Path) -> list[str]:
    provider = f'model_providers.stub={{name="stub",base_url="{url}/v1",wire_api="responses"}}'
    return [
        binary,
        "exec",
        "--skip-git-repo-check",
        # The flags a contained launch passes. Plugins off also stops codex cloning its plugin
        # catalog into its home at startup.
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
        *flags,
        "-C",
        str(cwd),
        _PROMPT,
    ]


def _decoy_toml(name: str, command: str, args: Sequence[str]) -> str:
    return f"[mcp_servers.{name}]\ncommand = {_toml_string(command)}\nargs = {_toml_array(args)}\n"


def _server_args(script: Path, log: Path) -> list[str]:
    return ["-I", str(script), str(log), TOOL]


def _run(
    binary: str,
    runner: Runner,
    name: str,
    settings: Sequence[str],
    *,
    ignore_user_config: bool,
    root: Path,
    python: str,
    script: Path,
) -> dict[str, Any]:
    """One codex run: what it was given, the tools each request offered, the methods each
    stand-in server received and the output of the ``ping`` call, None when none came back."""
    base = root / name
    home, cwd = base / "codex-home", base / "cwd"
    for path in (home, cwd / ".codex"):
        path.mkdir(parents=True)
    logs = {server: base / f"{server}.log" for server in _SERVERS}
    (home / "config.toml").write_text(
        _decoy_toml(_USER_DECOY, python, _server_args(script, logs[_USER_DECOY])), encoding="utf-8"
    )
    (cwd / ".codex" / "config.toml").write_text(
        _decoy_toml(_PROJECT_DECOY, python, _server_args(script, logs[_PROJECT_DECOY])),
        encoding="utf-8",
    )
    flags = mcp_flags(SERVER_NAME, python, _server_args(script, logs[SERVER_NAME]))
    if not ignore_user_config:
        flags.remove("--ignore-user-config")
    flags += list(settings)
    stand_in = ToolsStandIn()
    with read_probe._serve(stand_in) as url:
        argv = _argv(binary, url, flags, cwd)
        env = sandbox_probe._env(home)
        status = runner(argv, env, cwd)
    return {
        "settings": list(settings),
        "argv": argv,
        "env_names": sorted(env),
        "exit_status": status,
        "routes": list(stand_in.routes),
        "offered": [list(labels) for labels in stand_in.offered],
        "server_methods": {server: logged_methods(path) for server, path in logs.items()},
        "ping_result": stand_in.outputs.get("call_0"),
    }


def _union(run: Mapping[str, Any]) -> list[str]:
    return sorted({label for labels in run["offered"] for label in labels})


def _listed(labels: Sequence[str]) -> str:
    """Every label, whole: a list cut short hides the tool that fails the row."""
    return ", ".join(labels) or "nothing"


def _native_arm(native: Mapping[str, Any]) -> dict[str, Any]:
    offered = _union(native)
    natives = [label for label in offered if _owner(label) is None]
    has_server = any(_owner(label) == SERVER_NAME for label in offered)
    return {
        "passed": bool(natives),
        "evidence": (
            f"with no candidate the run offered {_listed(offered)}, "
            + ("with" if has_server else "without")
            + f" the server's tool; exit status {native['exit_status']}"
        ),
    }


def _row_arms(
    run: Mapping[str, Any], native_arm: Mapping[str, Any], ambient: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    arms: dict[str, dict[str, Any]] = {}
    offered = _union(run)
    others = [label for label in offered if _owner(label) != SERVER_NAME]
    with_server = sum(
        1 for labels in run["offered"] if any(_owner(label) == SERVER_NAME for label in labels)
    )
    arms["offers_only_server_tools"] = {
        "passed": with_server > 0 and not others,
        "evidence": (
            f"{len(run['offered'])} requests, {with_server} offering the server's tool; tools "
            f"offered across them: {_listed(offered)}; exit status {run['exit_status']}"
        ),
    }
    arms["control_offers_native_tools"] = dict(native_arm)

    ambient_offered = _union(ambient)
    # The control runs with codex's own tools on, which may hold MCP tools back from the array, so
    # a decoy server that was asked for its tools shows the decoy loaded as well as its tool does.
    loaded = [
        name
        for name in (_USER_DECOY, _PROJECT_DECOY)
        if any(_owner(label) == name for label in ambient_offered)
        or "tools/list" in ambient["server_methods"].get(name, [])
    ]
    control_ok = _USER_DECOY in loaded
    leaked = [label for label in offered if _owner(label) in (_USER_DECOY, _PROJECT_DECOY)]
    started = [name for name in (_USER_DECOY, _PROJECT_DECOY) if run["server_methods"].get(name)]
    arms["ambient_servers_absent"] = {
        "passed": control_ok and not leaked and not started,
        "evidence": (
            f"without --ignore-user-config the control offered {_listed(ambient_offered)} and "
            f"loaded the decoys {_listed(loaded)}"
            + (
                ", so the user-scope decoy is one this binary loads"
                if control_ok
                else ", without the user-scope decoy, so its absence below shows nothing"
            )
            + f"; with the candidate decoy tools offered: {_listed(leaked)}, decoy servers "
            f"started: {_listed(started)}"
        ),
    }

    methods = run["server_methods"].get(SERVER_NAME, [])
    ping = run["ping_result"]
    clipped = " ".join((ping or "no output").split())[:_EVIDENCE_CHARS]
    arms["server_connected"] = {
        "passed": {"initialize", "tools/list", "tools/call"} <= set(methods)
        and ping is not None
        and ANSWER in ping,
        "evidence": f"the server received {_listed(methods)}; the call returned: {clipped}",
    }
    return {name: arms[name] for name in REQUIRED_ARMS}


def _probe(
    binary: str, candidates: Sequence[str], runner: Runner, root: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    script = root / "mcp_stand_in.py"
    script.write_text(MCP_SERVER_SOURCE, encoding="utf-8")
    python = read_probe._child_python()
    common = {"root": root, "python": python, "script": script}
    native = _run(binary, runner, "native_control", (), ignore_user_config=True, **common)
    ambient = _run(binary, runner, "ambient_control", (), ignore_user_config=False, **common)
    native_arm = _native_arm(native)
    rows = []
    runs: dict[str, Any] = {"native_control": native, "ambient_control": ambient}
    for index, text in enumerate(candidates):
        settings = parse_candidate(text)
        run = _run(
            binary, runner, f"candidate-{index}", settings, ignore_user_config=True, **common
        )
        runs[f"candidate-{index}"] = run
        arms = _row_arms(run, native_arm, ambient)
        rows.append(
            {
                "candidate": text,
                "settings": settings,
                "offered": _union(run),
                "arms": arms,
                "passed": all(arm["passed"] for arm in arms.values()),
            }
        )
    return rows, runs


def _scratch_parent(scratch: Path | str | None) -> Path:
    """The directory the probe makes its root in, created when absent: a caller-given path, else
    ``<lionagi home>/state/codex-tools-probe``."""
    from lionagi import _paths

    chosen = (
        scratch
        if scratch is not None
        else Path(_paths.LIONAGI_HOME) / "state" / "codex-tools-probe"
    )
    parent = Path(os.path.realpath(os.path.expanduser(chosen)))
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def probe(
    binary: str,
    candidates: Sequence[str],
    *,
    runner: Runner | None = None,
    scratch: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the two controls and every candidate against ``binary``: one row per candidate, in
    the order given, and what each run was given and saw.

    Every candidate is split before any run starts, so a candidate that cannot be split runs
    nothing. ``runner`` defaults to the sandbox probe's ``run_codex``. ``scratch`` is a directory
    to make the probe's root in; the root is a fresh subdirectory, removed afterwards.
    """
    if not candidates:
        raise ValueError("name at least one candidate")
    for text in candidates:
        parse_candidate(text)
    parent = _scratch_parent(scratch)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        return _probe(binary, candidates, runner or sandbox_probe.run_codex, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def tools_record_dir() -> Path:
    """Where codex tools records live: ``LIONAGI_HOME/codex-tools-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "codex-tools-probes"


def tools_record_path(resolved: str, version: str, sha256: str) -> Path:
    """The record for one binary, named by its resolved path, version and the SHA-256 of its
    bytes: a binary replaced in place under the same path and version has no record."""
    digest = hashlib.sha256(f"{resolved}\0{version}\0{sha256}".encode()).hexdigest()[:24]
    return tools_record_dir() / f"codex-{version}-{digest}.json"


def write_record(binary: str, rows: Sequence[Mapping[str, Any]], runs: Mapping[str, Any]) -> Path:
    """Write the tools record for ``binary``, replacing any earlier one. ``mcp_flags`` holds the
    MCP flags measured, with ``{server}``, ``{command}`` and ``{args}`` where a launch puts its
    own."""
    resolved = os.path.realpath(binary)
    version = codex_binary_version(binary)
    if version is None:
        raise RuntimeError(f"the version of {binary!r} cannot be read")
    sha256 = binary_sha256(resolved)
    if sha256 is None:
        raise RuntimeError(f"the bytes of {binary!r} cannot be read")
    path = tools_record_path(resolved, version, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "binary": resolved,
        "version": version,
        "sha256": sha256,
        "mcp_flags": mcp_flags("{server}", "{command}", ["{args}"]),
        "rows": [dict(row) for row in rows],
        "runs": {name: dict(run) for name, run in runs.items()},
        "probed_at": datetime.now(UTC).isoformat(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the codex binary on PATH with each candidate and write the table; see the module
    docstring for the exit statuses."""
    parser = argparse.ArgumentParser(prog="python -m lionhub.providers.openai.codex_tools_probe")
    parser.add_argument("--binary", help="codex binary to probe (default: codex on PATH)")
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="codex arguments to measure, split as a POSIX shell splits them; repeat for more",
    )
    parser.add_argument(
        "--scratch",
        help=(
            "directory to make the probe's root in (default: "
            "<lionagi home>/state/codex-tools-probe)"
        ),
    )
    args = parser.parse_args(argv)
    if not args.candidate:
        print("name at least one --candidate", file=sys.stderr)
        return 2
    try:
        for text in args.candidate:
            parse_candidate(text)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    binary = args.binary or shutil.which("codex")
    if not binary:
        print("codex is not on PATH", file=sys.stderr)
        return 2
    try:
        rows, runs = probe(
            binary, args.candidate, scratch=Path(args.scratch) if args.scratch else None
        )
        path = write_record(binary, rows, runs)
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for row in rows:
        print(f"{'pass' if row['passed'] else 'FAIL'}  {row['candidate']}")
        print(f"      offered: {_listed(row['offered'])}")
        for name, arm in row["arms"].items():
            print(f"      {'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"record: {path}")
    if rows and not rows[0]["arms"]["control_offers_native_tools"]["passed"]:
        return 3
    return 0 if any(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
