# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Claude Code tools probe, driven without a claude binary.

A fake claude reads the tool flags off the command line, starts the MCP servers they leave it and
asks each for its tools over stdio, offers the Messages stand-in the tools it ends up with, and
calls a server tool when the stand-in asks. The stand-in MCP server is the probe's own script, run
for real; only the HTTP transport of the Messages stand-in is replaced by a function call, because
some hosts refuse to bind even a loopback port. Named leaks break one behaviour at a time, so each
arm is shown to fail for the reason it exists.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import stat
import subprocess
from pathlib import Path

import pytest

from lionhub.providers.anthropic import claude_read_probe as read_probe
from lionhub.providers.anthropic import claude_tools_probe as probe_module
from lionhub.providers.anthropic.claude_tools_probe import (
    REQUIRED_ARMS,
    SERVER_NAME,
    ToolListStandIn,
    tools_off_flags,
)

_PORTS = itertools.count(42000)
_LIVE: dict[str, object] = {}
_NATIVE = ("Bash", "Edit", "Read")


@contextlib.contextmanager
def _in_memory_serve(app):
    """The probe's listener without the socket: the fake claude calls the app directly."""
    url = f"http://127.0.0.1:{next(_PORTS)}"
    _LIVE[url] = app
    try:
        yield url
    finally:
        _LIVE.pop(url, None)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Records under the test's own LIONAGI_HOME and the in-memory listener."""
    import lionagi._paths as paths

    monkeypatch.setattr(paths, "LIONAGI_HOME", tmp_path / "lionagi-home")
    monkeypatch.setattr(read_probe, "_serve", _in_memory_serve)


# ------------------------------------------------------------------------------- the fake claude
def _list_value(argv: list[str], flag: str) -> list[str] | None:
    """The values a list flag takes: the arguments after it up to the next flag."""
    if flag not in argv:
        return None
    values = []
    for arg in argv[argv.index(flag) + 1 :]:
        if arg.startswith("--"):
            break
        values.append(arg)
    return values


def _tool_call(payload: bytes) -> tuple[str, str] | None:
    """The tool call a streamed reply carries, as (id, name), or None for text."""
    for chunk in payload.decode().split("\n\n"):
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: ") :])
            if (
                event["type"] == "content_block_start"
                and event["content_block"]["type"] == "tool_use"
            ):
                return event["content_block"]["id"], event["content_block"]["name"]
    return None


class _Client:
    """One stdio MCP server, started from its definition and spoken to line by line."""

    def __init__(self, spec: dict) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 -- the probe's own server script
            [spec["command"], *spec["args"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self._ids = itertools.count(1)
        self._request("initialize", {"protocolVersion": "2025-06-18"})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict) -> dict:
        self._send({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params})
        return json.loads(self.proc.stdout.readline())["result"]

    def tools(self) -> list[str]:
        return [tool["name"] for tool in self._request("tools/list", {})["tools"]]

    def call(self, tool: str) -> str:
        result = self._request("tools/call", {"name": tool, "arguments": {}})
        return "".join(part["text"] for part in result["content"])

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=20)
        self.proc.stdout.close()


class FakeClaude:
    """A claude that honours its tool flags, with named leaks.

    Leaks: ``tools_ignored`` (``--tools ""`` leaves the built-in tools), ``late_native`` (a later
    request offers ``Bash``), ``strict_ignored`` (the ambient servers load under
    ``--strict-mcp-config``), ``decoy_never_loads`` (ambient servers never load),
    ``server_not_started`` (the server's tool is offered but its server never started),
    ``rejects_empty_tools`` (the CLI refuses ``--tools ""`` and exits before any request) and
    ``no_native_control`` (a run without ``--tools`` offers no built-in tool).
    """

    def __init__(self, *leaks: str) -> None:
        self.leaks = set(leaks)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> int:
        self.calls.append(list(argv))
        tools = _list_value(argv, "--tools")
        if tools == [""] and "rejects_empty_tools" in self.leaks:
            return 1
        if tools is None:
            native = [] if "no_native_control" in self.leaks else list(_NATIVE)
        elif "tools_ignored" in self.leaks:
            native = list(_NATIVE)
        else:
            native = [tool for tool in tools if tool]
        servers: dict[str, dict] = {}
        for config in _list_value(argv, "--mcp-config") or []:
            servers.update(json.loads(Path(config).read_text())["mcpServers"])
        strict = "--strict-mcp-config" in argv
        if (not strict or "strict_ignored" in self.leaks) and "decoy_never_loads" not in self.leaks:
            for path in (
                Path(env["HOME"]) / ".claude.json",
                Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json",
            ):
                if path.is_file():
                    servers.update(json.loads(path.read_text())["mcpServers"])
        clients: dict[str, _Client] = {}
        server_tools: list[str] = []
        try:
            for name, spec in servers.items():
                if name == SERVER_NAME and "server_not_started" in self.leaks:
                    server_tools.append(f"mcp__{name}__ping")
                    continue
                clients[name] = _Client(spec)
                server_tools += [f"mcp__{name}__{tool}" for tool in clients[name].tools()]
            stand_in = _LIVE[env["ANTHROPIC_BASE_URL"]]
            results: list[dict] = []
            for turn in range(8):
                offered = native + server_tools
                if turn > 0 and "late_native" in self.leaks:
                    offered = [*offered, "Bash"]
                body = {
                    "model": argv[argv.index("--model") + 1],
                    "stream": True,
                    "tools": [{"name": name} for name in offered],
                    "messages": [
                        {"role": "user", "content": argv[-1]},
                        {"role": "user", "content": results},
                    ],
                }
                status, _, payload = stand_in.answer(
                    "POST", "/v1/messages?beta=true", json.dumps(body).encode()
                )
                assert status == 200
                call = _tool_call(payload)
                if call is None:
                    return 0
                call_id, name = call
                _, server, tool = name.split("__")
                if server in clients:
                    text, is_error = clients[server].call(tool), False
                else:
                    text, is_error = f"MCP server {server} is not connected", True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            raise AssertionError("the stand-in never ended the run")
        finally:
            for client in clients.values():
                client.close()


def _probe(tmp_path: Path, *leaks: str) -> tuple[dict, dict, FakeClaude]:
    fake = FakeClaude(*leaks)
    arms, runs = probe_module.probe("/fake/claude", runner=fake, scratch=tmp_path / "scratch")
    return arms, runs, fake


def _failed(arms: dict) -> set[str]:
    return {name for name, arm in arms.items() if not arm["passed"]}


# ----------------------------------------------------------------------------------- the arms
def test_a_binary_that_honours_the_flags_passes_every_arm(tmp_path):
    arms, runs, fake = _probe(tmp_path)

    assert list(arms) == list(REQUIRED_ARMS)
    assert _failed(arms) == set(), arms
    off = runs["tools_off"]
    assert off["offered"] == [["mcp__probe__ping"], ["mcp__probe__ping"]]
    assert off["server_methods"][SERVER_NAME][:2] == ["initialize", "notifications/initialized"]
    assert "tools/call" in off["server_methods"][SERVER_NAME]
    assert off["server_methods"]["decoyuser"] == []
    assert "mcp__decoyuser__ping" in runs["ambient_control"]["offered"][0]
    assert "Bash" in runs["native_control"]["offered"][0]
    assert len(fake.calls) == 3


def test_the_measured_run_carries_the_launch_flags_and_each_control_drops_one(tmp_path):
    _, runs, _ = _probe(tmp_path)

    off = runs["tools_off"]["argv"]
    config = off[off.index("--mcp-config") + 1]
    start = off.index("--tools")
    assert off[start : start + 7] == tools_off_flags(config, SERVER_NAME)
    assert off[start + 7] == "--permission-prompts"
    assert "--bare" not in off
    assert "--tools" not in runs["native_control"]["argv"]
    assert "--strict-mcp-config" in runs["native_control"]["argv"]
    assert "--strict-mcp-config" not in runs["ambient_control"]["argv"]
    assert _list_value(runs["ambient_control"]["argv"], "--tools") == [""]


@pytest.mark.parametrize(
    ("leak", "expected"),
    [
        ("tools_ignored", {"offers_only_server_tools"}),
        ("late_native", {"offers_only_server_tools"}),
        ("strict_ignored", {"offers_only_server_tools", "ambient_servers_absent"}),
        ("decoy_never_loads", {"ambient_servers_absent"}),
        ("server_not_started", {"server_connected"}),
        (
            "rejects_empty_tools",
            {"offers_only_server_tools", "ambient_servers_absent", "server_connected"},
        ),
        ("no_native_control", {"control_offers_native_tools"}),
    ],
)
def test_each_leak_fails_the_arm_that_exists_for_it(tmp_path, leak, expected):
    arms, _, _ = _probe(tmp_path, leak)

    assert _failed(arms) == expected, arms


def test_a_run_with_no_request_fails_rather_than_reading_as_no_tools(tmp_path):
    def silent(argv, env, cwd):
        return 0

    arms, runs = probe_module.probe("/fake/claude", runner=silent, scratch=tmp_path / "s")

    assert _failed(arms) == set(REQUIRED_ARMS)
    assert runs["tools_off"]["offered"] == []
    assert "0 requests" in arms["offers_only_server_tools"]["evidence"]


def test_the_stand_in_keeps_tool_names_and_nothing_else(tmp_path):
    stand_in = ToolListStandIn([])
    body = {
        "tools": [{"name": "Bash", "input_schema": {"secret": "x"}}, {"name": "Read"}],
        "system": "not kept",
        "messages": [],
    }
    stand_in.answer("POST", "/v1/messages", json.dumps(body).encode())
    stand_in.answer("POST", "/v1/messages", json.dumps({"messages": []}).encode())
    stand_in.answer("POST", "/v1/messages/count_tokens", b"{}")

    assert stand_in.offered == [["Bash", "Read"], []]
    assert "not kept" not in json.dumps(vars(stand_in), default=str)


def test_the_flags_put_the_empty_tool_list_before_the_next_flag():
    assert tools_off_flags("/x/mcp.json", "kc") == [
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        "/x/mcp.json",
        "--allowed-tools",
        "mcp__kc",
    ]


# --------------------------------------------------------------------------------- the record
def _fake_binary(path: Path, version: str) -> Path:
    path.write_text(f"#!/bin/sh\necho '{version} (Claude Code)'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_record_is_keyed_by_the_binary_and_holds_the_measured_flags(tmp_path):
    arms, runs, _ = _probe(tmp_path)
    binary = _fake_binary(tmp_path / "claude", "2.1.400")

    path = probe_module.write_record(str(binary), arms, runs)

    assert path.parent == probe_module.tools_record_dir()
    assert path.name.startswith("claude-2.1.400-")
    record = json.loads(path.read_text())
    assert record["format"] == 1
    assert record["version"] == "2.1.400"
    assert record["tool_flags"] == tools_off_flags("{mcp_config}", "{server}")
    assert set(record["arms"]) == set(REQUIRED_ARMS)
    assert set(record["runs"]) == {"tools_off", "native_control", "ambient_control"}

    other = _fake_binary(tmp_path / "claude-other", "2.1.400")
    other.write_text(other.read_text() + "# other bytes\n")
    assert probe_module.write_record(str(other), arms, runs) != path


def test_a_binary_whose_version_cannot_be_read_gets_no_record(tmp_path):
    arms, runs, _ = _probe(tmp_path)
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 3\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(RuntimeError, match="version"):
        probe_module.write_record(str(binary), arms, runs)
    assert not probe_module.tools_record_dir().exists()


# ----------------------------------------------------------------------------------- the CLI
def test_main_without_a_claude_on_path_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(probe_module.shutil, "which", lambda name: None)

    assert probe_module.main([]) == 2
    assert "claude is not on PATH" in capsys.readouterr().err


@pytest.mark.parametrize(("passed", "status"), [(True, 0), (False, 1)])
def test_main_exits_0_only_when_every_arm_passed(monkeypatch, tmp_path, passed, status):
    arms = {name: {"passed": True, "evidence": "e"} for name in REQUIRED_ARMS}
    arms["server_connected"]["passed"] = passed
    monkeypatch.setattr(probe_module, "probe", lambda binary, scratch=None: (arms, {}))
    monkeypatch.setattr(probe_module, "write_record", lambda binary, a, r: tmp_path / "record.json")

    assert probe_module.main(["--binary", "/fake/claude"]) == status
