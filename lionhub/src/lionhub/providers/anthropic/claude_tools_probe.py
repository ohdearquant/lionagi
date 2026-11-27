# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Whether Claude Code offers its model only one MCP server's tools, for one ``claude`` binary.

ADR-0041 Amendment 9 (D2) lets a claude_code leg run on a computer host only when the binary the
host resolves has a record showing that the launch's tool flags, :func:`tools_off_flags`, leave
the model no tool but those of the one MCP server the host wrote. The proof is the list of tools
in the model requests the CLI sends, never the CLI's own report of its tools. This probe makes
that record.

It writes a stdio stand-in MCP server that offers one tool, ``ping``, answers it with ``pong``
and logs every method it receives. It then runs the binary three times against the read probe's
loopback stand-in for the Messages API (:mod:`lionhub.providers.anthropic.claude_read_probe`),
which here also keeps the name of every tool each request offers. The stand-in answers a request
that offers tools with one call of ``mcp__probe__ping`` and the next with text. Each run has its
own working directory, ``HOME`` and ``CLAUDE_CONFIG_DIR``, and in each two decoy servers are named
where Claude Code finds ambient configuration: one in the run's ``~/.claude.json`` and
``CLAUDE_CONFIG_DIR/.claude.json``, one in a ``.mcp.json`` in its working directory. The runs:

- ``tools_off``: the launch's flags, with the stand-in server as the only one in ``--mcp-config``;
- ``native_control``: the same without ``--tools ""``;
- ``ambient_control``: the same as ``tools_off`` without ``--strict-mcp-config``.

The arms:

- ``offers_only_server_tools``: ``tools_off`` sent at least one request offering
  ``mcp__probe__ping``, and no request, the first or any later one, offered any other tool.
- ``control_offers_native_tools``: ``native_control`` offered ``Bash`` beside the server's tool,
  so a run that offers no built-in tool is not a blind read of the tool list.
- ``ambient_servers_absent``: ``ambient_control`` offered the user-scope decoy's tool, which shows
  this binary loads that decoy, and ``tools_off`` offered neither decoy's tool and started neither
  decoy server. The project decoy is checked for absence only: whether this binary would load an
  unapproved project server at all is not shown.
- ``server_connected``: in ``tools_off`` the stand-in server received ``initialize``,
  ``tools/list`` and ``tools/call``, and the call of ``ping`` came back with its answer.

The child gets no ``--bare``, since a leg runs without it and the record is of the command line a
launch uses. It gets the read probe's environment: the stand-in as ``ANTHROPIC_BASE_URL``, a
placeholder API key, and proxy variables pointed at a listener that refuses every request. Every
model request goes to the stand-in, which never keeps headers; what reached the proxy listener is
kept in the record. The record is written to ``LIONAGI_HOME/claude-tools-probes``, keyed by the
binary's resolved path, version and bytes, with the flags it measured. No launch reads it yet.

Run ``python -m lionhub.providers.anthropic.claude_tools_probe``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lionagi._errors import ConfigurationError
from lionhub.providers.anthropic import claude_read_probe as read_probe

__all__ = (
    "REQUIRED_ARMS",
    "SERVER_NAME",
    "ToolListStandIn",
    "main",
    "probe",
    "tools_off_flags",
    "tools_record_dir",
    "tools_record_path",
    "write_record",
)

_RECORD_FORMAT = 1

#: Runs one claude command line, ``(argv, env, cwd)``, and returns its exit status, -1 when it
#: outlived the timeout. Injected so tests need no binary.
Runner = Callable[[list[str], dict[str, str], Path], int]

#: The name the probe gives its one MCP server.
SERVER_NAME = "probe"
_TOOL = "ping"
_ANSWER = "pong"
_USER_DECOY = "decoyuser"
_PROJECT_DECOY = "decoyproject"


def _mcp_tool(server: str) -> str:
    """The name Claude Code offers the model for ``server``'s ``ping`` tool."""
    return f"mcp__{server}__{_TOOL}"


#: Every arm a record must hold as passed for the launch's flags to count as leaving the model
#: only the one server's tools.
REQUIRED_ARMS: tuple[str, ...] = (
    "offers_only_server_tools",
    "control_offers_native_tools",
    "ambient_servers_absent",
    "server_connected",
)

_RUNS: tuple[str, ...] = ("tools_off", "native_control", "ambient_control")

#: The stdio stand-in MCP server: ``python -I <this file> <log> <tool>``. It writes every method
#: it receives to the log, one JSON object per line, and nothing but protocol to its output.
_MCP_SERVER_SOURCE = """import json
import sys

LOG, TOOL = sys.argv[1], sys.argv[2]


def log(method):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"method": method}) + "\\n")


def reply(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if not isinstance(message, dict):
        continue
    method = message.get("method")
    log(method)
    if "id" not in message:
        continue
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "lionagi-tools-probe", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": TOOL,
                    "description": "Answers pong.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "pong"}]}
    elif method == "ping":
        result = {}
    else:
        reply(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "method not found"},
            }
        )
        continue
    reply({"jsonrpc": "2.0", "id": message["id"], "result": result})
"""


def tools_off_flags(mcp_config: str, server: str) -> list[str]:
    """The flags that leave a claude_code child no tool but those of ``server``, the one server
    the file ``mcp_config`` defines.

    ``--tools ""`` is meant to remove every built-in tool, ``--strict-mcp-config`` to keep Claude
    Code from reading servers from anywhere but ``--mcp-config``, and ``--allowed-tools`` lets the
    child call ``server``'s tools without a permission prompt; the record says whether they do.
    ``--tools``, ``--mcp-config`` and ``--allowed-tools`` each take a list, so a caller follows
    these flags with another flag, never with the prompt.
    """
    return [
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config,
        "--allowed-tools",
        f"mcp__{server}",
    ]


class ToolListStandIn(read_probe.MessagesStandIn):
    """The read probe's Messages stand-in, which also keeps the names of the tools each request
    offers, in request order. It keeps no other part of a request."""

    def __init__(self, steps: Sequence[Mapping[str, Any]]) -> None:
        super().__init__(steps)
        self.offered: list[list[str]] = []

    def answer(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        route = path.split("?", 1)[0]
        if method == "POST" and route.endswith("/messages"):
            try:
                request = json.loads(body or b"{}")
            except ValueError:
                request = {}
            tools = request.get("tools") if isinstance(request, dict) else None
            names = (
                sorted(str(tool.get("name")) for tool in tools if isinstance(tool, dict))
                if isinstance(tools, list)
                else []
            )
            with self._lock:
                self.offered.append(names)
        return super().answer(method, path, body)


def _flags(run: str, mcp_config: str) -> list[str]:
    """The tool flags of one run: the launch's, or a control's, which drops one of them."""
    flags = tools_off_flags(mcp_config, SERVER_NAME)
    if run == "native_control":
        at = flags.index("--tools")
        del flags[at : at + 2]
    elif run == "ambient_control":
        flags.remove("--strict-mcp-config")
    return flags


def _argv(binary: str, flags: Sequence[str]) -> list[str]:
    # --permission-prompts takes one value, so the prompt may follow it.
    return [
        binary,
        "-p",
        "--no-session-persistence",
        "--model",
        read_probe._MODEL,
        *flags,
        "--permission-prompts",
        "none",
        read_probe._PROMPT,
    ]


def _server(python: str, script: Path, log: Path) -> dict[str, Any]:
    return {"type": "stdio", "command": python, "args": ["-I", str(script), str(log), _TOOL]}


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _logged(path: Path) -> list[str]:
    """The methods a stand-in server logged, in order; none when it never started."""
    if not path.is_file():
        return []
    methods = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("method"), str):
            methods.append(entry["method"])
    return methods


def _run(
    binary: str, runner: Runner, run: str, root: Path, python: str, script: Path
) -> dict[str, Any]:
    """One claude run: what it was given, the tools each request offered, the methods each
    stand-in server received and the result of the ``ping`` call, None when none came back."""
    base = root / run
    home, config, cwd = base / "home", base / "config", base / "cwd"
    # A short name: Claude Code is documented to give Bash the system temp directory instead of an
    # override path it finds too long.
    claude_tmp = root / f"t{_RUNS.index(run)}"
    for path in (home, config, cwd, claude_tmp):
        path.mkdir(parents=True)
    logs = {name: base / f"{name}.log" for name in (SERVER_NAME, _USER_DECOY, _PROJECT_DECOY)}
    mcp_config = base / "mcp.json"
    _write_json(
        mcp_config, {"mcpServers": {SERVER_NAME: _server(python, script, logs[SERVER_NAME])}}
    )
    user = {"mcpServers": {_USER_DECOY: _server(python, script, logs[_USER_DECOY])}}
    _write_json(home / ".claude.json", user)
    _write_json(config / ".claude.json", user)
    project = {"mcpServers": {_PROJECT_DECOY: _server(python, script, logs[_PROJECT_DECOY])}}
    _write_json(cwd / ".mcp.json", project)

    stand_in = ToolListStandIn([{"tool": _mcp_tool(SERVER_NAME), "input": {}}])
    proxy = read_probe.ProxyRecorder()
    argv = _argv(binary, _flags(run, str(mcp_config)))
    with read_probe._serve(stand_in) as base_url, read_probe._serve(proxy) as proxy_url:
        env = read_probe._env(base_url, proxy_url, home, config, claude_tmp)
        status = runner(argv, env, cwd)
    return {
        "argv": argv,
        "env_names": sorted(env),
        "exit_status": status,
        "requests": list(stand_in.seen),
        "offered": [list(names) for names in stand_in.offered],
        "proxied": list(proxy.seen),
        "server_methods": {name: _logged(path) for name, path in logs.items()},
        "ping_result": stand_in.results.get("toolu_probe_0"),
    }


def _union(run: Mapping[str, Any]) -> list[str]:
    return sorted({name for names in run["offered"] for name in names})


def _clip(names: Sequence[str]) -> str:
    return (", ".join(names) or "nothing")[: read_probe._EVIDENCE_CHARS]


def _arms(runs: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    off, native, ambient = runs["tools_off"], runs["native_control"], runs["ambient_control"]
    server_tool = _mcp_tool(SERVER_NAME)
    decoys = (_mcp_tool(_USER_DECOY), _mcp_tool(_PROJECT_DECOY))
    arms: dict[str, dict[str, Any]] = {}

    offered = _union(off)
    others = [name for name in offered if name != server_tool]
    with_server = sum(1 for names in off["offered"] if server_tool in names)
    arms["offers_only_server_tools"] = {
        "passed": with_server > 0 and not others,
        "evidence": (
            f"{len(off['offered'])} requests, {with_server} offering {server_tool}; tools offered "
            f"across them: {_clip(offered)}; exit status {off['exit_status']}"
        ),
    }

    native_offered = _union(native)
    arms["control_offers_native_tools"] = {
        "passed": "Bash" in native_offered and server_tool in native_offered,
        "evidence": (
            f"without --tools the run offered {_clip(native_offered)}; exit status "
            f"{native['exit_status']}"
        ),
    }

    ambient_offered = _union(ambient)
    control_ok = decoys[0] in ambient_offered
    started = [name for name in (_USER_DECOY, _PROJECT_DECOY) if off["server_methods"].get(name)]
    leaked = [name for name in offered if name in decoys]
    arms["ambient_servers_absent"] = {
        "passed": control_ok and not leaked and not started,
        "evidence": (
            f"without --strict-mcp-config the run offered {_clip(ambient_offered)}"
            + (
                ", so the user-scope decoy is one this binary loads"
                if control_ok
                else ", without the user-scope decoy, so its absence below shows nothing"
            )
            + f"; with the launch's flags decoy tools offered: {_clip(leaked)}, decoy servers "
            f"started: {_clip(started)}; the project decoy is checked for absence only"
        ),
    }

    methods = off["server_methods"].get(SERVER_NAME, [])
    ping = off["ping_result"]
    answered = ping is not None and not ping[1] and _ANSWER in ping[0]
    arms["server_connected"] = {
        "passed": {"initialize", "tools/list", "tools/call"} <= set(methods) and answered,
        "evidence": (f"the server received {_clip(methods)}; " + read_probe._evidence(ping)),
    }
    return arms


def _probe(
    binary: str, runner: Runner, root: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    script = root / "mcp_stand_in.py"
    script.write_text(_MCP_SERVER_SOURCE, encoding="utf-8")
    python = read_probe._child_python()
    runs = {run: _run(binary, runner, run, root, python, script) for run in _RUNS}
    arms = _arms(runs)
    return {name: arms[name] for name in REQUIRED_ARMS}, runs


def _scratch_parent(scratch: Path | str | None) -> Path:
    """The directory the probe makes its root in, created when absent: a caller-given path, else
    ``<lionagi home>/state/claude-tools-probe``."""
    from lionagi import _paths

    chosen = (
        scratch
        if scratch is not None
        else Path(_paths.LIONAGI_HOME) / "state" / "claude-tools-probe"
    )
    parent = Path(os.path.realpath(os.path.expanduser(chosen)))
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def probe(
    binary: str,
    *,
    runner: Runner | None = None,
    scratch: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Run every arm against ``binary``: arm name to ``{"passed": bool, "evidence": str}``, and
    what each of the three runs was given and saw.

    ``runner`` defaults to the read probe's ``run_claude``. ``scratch`` is a directory to make the
    probe's root in; the root is a fresh subdirectory, removed afterwards.
    """
    parent = _scratch_parent(scratch)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        return _probe(binary, runner or read_probe.run_claude, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def tools_record_dir() -> Path:
    """Where tools records live: ``LIONAGI_HOME/claude-tools-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "claude-tools-probes"


def tools_record_path(resolved: str, version: str, sha256: str) -> Path:
    """The record for one binary, named by its resolved path, version and the SHA-256 of its
    bytes: a binary replaced in place under the same path and version has no record."""
    digest = hashlib.sha256(f"{resolved}\0{version}\0{sha256}".encode()).hexdigest()[:24]
    return tools_record_dir() / f"claude-{version}-{digest}.json"


def write_record(
    binary: str,
    arms: Mapping[str, Mapping[str, Any]],
    runs: Mapping[str, Any],
) -> Path:
    """Write the tools record for ``binary``, replacing any earlier one. ``tool_flags`` holds the
    flags measured, with ``{mcp_config}`` and ``{server}`` where a launch puts its own."""
    resolved = os.path.realpath(binary)
    version = read_probe._binary_version(binary)
    if version is None:
        raise RuntimeError(f"the version of {binary!r} cannot be read")
    sha256 = read_probe._sha256(resolved)
    if sha256 is None:
        raise RuntimeError(f"the bytes of {binary!r} cannot be read")
    path = tools_record_path(resolved, version, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "binary": resolved,
        "version": version,
        "sha256": sha256,
        "tool_flags": tools_off_flags("{mcp_config}", "{server}"),
        "runs": {name: dict(run) for name, run in runs.items()},
        "probed_at": datetime.now(UTC).isoformat(),
        "arms": {name: dict(arm) for name, arm in arms.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the claude binary on PATH and write its tools record; 0 only when every arm passed."""
    parser = argparse.ArgumentParser(
        prog="python -m lionhub.providers.anthropic.claude_tools_probe"
    )
    parser.add_argument("--binary", help="claude binary to probe (default: claude on PATH)")
    parser.add_argument(
        "--scratch",
        help=(
            "directory to make the probe's root in (default: "
            "<lionagi home>/state/claude-tools-probe)"
        ),
    )
    args = parser.parse_args(argv)
    binary = args.binary or shutil.which("claude")
    if not binary:
        print("claude is not on PATH", file=sys.stderr)
        return 2
    try:
        arms, runs = probe(binary, scratch=Path(args.scratch) if args.scratch else None)
        path = write_record(binary, arms, runs)
    except (ConfigurationError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for name, arm in arms.items():
        print(f"{'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"record: {path}")
    return 0 if all(arm["passed"] for arm in arms.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
