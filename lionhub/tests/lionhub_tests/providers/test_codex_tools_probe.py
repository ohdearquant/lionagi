# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The codex tools probe, driven without a codex binary.

A fake codex reads the MCP flags and the candidate's settings off the command line, starts the MCP
servers they leave it and asks each for its tools over stdio, offers the Responses stand-in a
tools array, and calls a server tool when the stand-in asks. Which settings remove which native
tool is the fake's own invention: the real candidates come from the installed CLI's reference,
and this file only shows that the probe reads the array the way its arms say. The stand-in MCP
server is the probe's own script, run for real; only the HTTP transport of the Responses stand-in
is replaced by a function call, because some hosts refuse to bind even a loopback port.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import re
import stat
import subprocess
from pathlib import Path

import pytest

from lionhub.providers.anthropic import claude_read_probe as read_probe
from lionhub.providers.openai import codex_tools_probe as probe_module
from lionhub.providers.openai.codex_tools_probe import (
    REQUIRED_ARMS,
    SERVER_NAME,
    ToolsStandIn,
    mcp_flags,
    parse_candidate,
    tool_labels,
)

_PORTS = itertools.count(43000)
_LIVE: dict[str, object] = {}

# The fake's own switches, one per native tool.
_SWITCHES = {
    "features.shell_tool=false": "exec_command",
    "features.apply_patch_freeform=false": "apply_patch",
    "tools.web_search=false": "web_search",
}
_ALL_OFF = " ".join(f"-c {switch}" for switch in _SWITCHES)


@contextlib.contextmanager
def _in_memory_serve(app):
    """The probe's listener without the socket: the fake codex calls the app directly."""
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


# -------------------------------------------------------------------------------- the fake codex
class _Client:
    """One stdio MCP server, started from its command and spoken to line by line."""

    def __init__(self, command: str, args: list[str]) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 -- the probe's own server script
            [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
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


def _overrides(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]


def _config_servers(path: Path) -> dict[str, tuple[str, list[str]]]:
    """The servers a config.toml the probe wrote defines (its values are JSON-shaped)."""
    if not path.is_file():
        return {}
    found = {}
    text = path.read_text()
    for name, command, args in re.findall(
        r"\[mcp_servers\.(\w+)\]\ncommand = (.+)\nargs = (.+)\n", text
    ):
        found[name] = (json.loads(command), json.loads(args))
    return found


def _reply_item(payload: bytes) -> dict:
    for chunk in payload.decode().split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                event = json.loads(line[len("data: ") :])
                if event["type"] == "response.output_item.done":
                    return event["item"]
    raise AssertionError("the stand-in sent no item")


class FakeCodex:
    """A codex that honours its flags and the fake switches, with named leaks.

    Leaks: ``switch_ignored`` (the switches remove nothing), ``late_native`` (a later request
    offers ``exec_command``), ``ignore_ignored`` (the user config loads under
    ``--ignore-user-config``), ``decoy_never_loads`` (the user config never loads),
    ``server_not_started`` (the server's tool is offered but its server never starts),
    ``no_native_control`` (no run ever offers a native tool) and ``namespaced`` (MCP tools come
    nested under a namespace tool, which is not a leak).
    """

    def __init__(self, *leaks: str) -> None:
        self.leaks = set(leaks)
        self.calls: list[list[str]] = []

    def _native(self, overrides: list[str]) -> list[dict]:
        if "no_native_control" in self.leaks:
            return []
        removed = set() if "switch_ignored" in self.leaks else set(overrides)
        tools = []
        for switch, name in _SWITCHES.items():
            if switch in removed:
                continue
            tools.append({"type": "web_search"} if name == "web_search" else {"name": name})
        return tools

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> int:
        self.calls.append(list(argv))
        overrides = _overrides(argv)
        servers: dict[str, tuple[str, list[str]]] = {}
        named: dict[str, dict] = {}
        for value in overrides:
            found = re.fullmatch(r"mcp_servers\.(\w+)\.(command|args)=(.+)", value)
            if found:
                named.setdefault(found.group(1), {})[found.group(2)] = json.loads(found.group(3))
        for name, spec in named.items():
            servers[name] = (spec["command"], spec["args"])
        ignore = "--ignore-user-config" in argv and "ignore_ignored" not in self.leaks
        if not ignore and "decoy_never_loads" not in self.leaks:
            servers.update(_config_servers(Path(env["CODEX_HOME"]) / "config.toml"))
        native = self._native(overrides)
        clients: dict[str, _Client] = {}
        mcp_tools: list[dict] = []
        by_call: dict[str, tuple[str, str]] = {}
        try:
            for name, (command, args) in servers.items():
                if name == SERVER_NAME and "server_not_started" in self.leaks:
                    tools = ["ping"]
                else:
                    clients[name] = _Client(command, args)
                    tools = clients[name].tools()
                if "namespaced" in self.leaks:
                    mcp_tools.append(
                        {
                            "type": "namespace",
                            "name": f"mcp__{name}",
                            "tools": [{"type": "function", "name": tool} for tool in tools],
                        }
                    )
                    by_call.update({tool: (name, tool) for tool in tools if name == SERVER_NAME})
                else:
                    for tool in tools:
                        mcp_tools.append({"type": "function", "name": f"mcp__{name}__{tool}"})
                        by_call[f"mcp__{name}__{tool}"] = (name, tool)
            provider = next(v for v in overrides if v.startswith("model_providers.stub="))
            url = re.search(r'base_url="([^"]+)/v1"', provider).group(1)
            stand_in = _LIVE[url]
            items: list[dict] = [{"type": "message", "role": "user", "content": argv[-1]}]
            for turn in range(6):
                offered = native + mcp_tools
                if turn > 0 and "late_native" in self.leaks:
                    offered = [*offered, {"name": "exec_command"}]
                body = {"model": "stub-model", "instructions": "not kept", "tools": offered}
                body["input"] = items
                status, _, payload = stand_in.answer(
                    "POST", "/v1/responses", json.dumps(body).encode()
                )
                assert status == 200
                item = _reply_item(payload)
                if item["type"] != "function_call":
                    return 0
                server, tool = by_call.get(item["name"], (None, None))
                if server in clients:
                    output = clients[server].call(tool)
                else:
                    output = f"unknown tool {item['name']}"
                items += [
                    item,
                    {"type": "function_call_output", "call_id": item["call_id"], "output": output},
                ]
            raise AssertionError("the stand-in never ended the run")
        finally:
            for client in clients.values():
                client.close()


def _probe(tmp_path: Path, *leaks: str, candidates=(_ALL_OFF,)):
    fake = FakeCodex(*leaks)
    rows, runs = probe_module.probe(
        "/fake/codex", list(candidates), runner=fake, scratch=tmp_path / "scratch"
    )
    return rows, runs, fake


def _failed(row: dict) -> set[str]:
    return {name for name, arm in row["arms"].items() if not arm["passed"]}


# ----------------------------------------------------------------------------------- the rows
def test_a_candidate_that_removes_every_native_tool_passes_every_arm(tmp_path):
    rows, runs, fake = _probe(tmp_path)

    (row,) = rows
    assert list(row["arms"]) == list(REQUIRED_ARMS)
    assert _failed(row) == set(), row
    assert row["passed"] is True
    assert row["offered"] == ["mcp__probe__ping"]
    run = runs["candidate-0"]
    assert run["offered"] == [["mcp__probe__ping"], ["mcp__probe__ping"]]
    assert run["server_methods"][SERVER_NAME][:2] == ["initialize", "notifications/initialized"]
    assert "tools/call" in run["server_methods"][SERVER_NAME]
    assert run["server_methods"]["decoyuser"] == []
    assert run["ping_result"] == "pong"
    assert "exec_command" in runs["native_control"]["offered"][0]
    assert "mcp__decoyuser__ping" in runs["ambient_control"]["offered"][0]
    assert len(fake.calls) == 3


def test_each_candidate_is_one_run_and_one_row_in_the_order_given(tmp_path):
    candidates = ("-c tools.web_search=false", _ALL_OFF)
    rows, runs, fake = _probe(tmp_path, candidates=candidates)

    assert [row["candidate"] for row in rows] == list(candidates)
    assert rows[0]["settings"] == ["-c", "tools.web_search=false"]
    assert rows[0]["offered"] == ["apply_patch", "exec_command", "mcp__probe__ping"]
    assert _failed(rows[0]) == {"offers_only_server_tools"}
    assert _failed(rows[1]) == set()
    assert set(runs) == {"native_control", "ambient_control", "candidate-0", "candidate-1"}
    assert len(fake.calls) == 4


def test_the_runs_carry_the_launch_mcp_flags_and_the_ambient_control_drops_one(tmp_path):
    _, runs, _ = _probe(tmp_path)

    measured = runs["candidate-0"]["argv"]
    start = measured.index("--ignore-user-config")
    assert measured[start + 5 : start + 11] == parse_candidate(_ALL_OFF)
    assert measured[measured.index("-C") + 2 :] == [measured[-1]]
    assert "--ignore-user-config" in runs["native_control"]["argv"]
    assert "--ignore-user-config" not in runs["ambient_control"]["argv"]
    assert runs["native_control"]["settings"] == []
    for name in ("apps", "plugins"):
        assert measured[measured.index(name) - 1] == "--disable"


@pytest.mark.parametrize(
    ("leak", "expected"),
    [
        ("switch_ignored", {"offers_only_server_tools"}),
        ("late_native", {"offers_only_server_tools"}),
        ("ignore_ignored", {"offers_only_server_tools", "ambient_servers_absent"}),
        ("decoy_never_loads", {"ambient_servers_absent"}),
        ("server_not_started", {"server_connected"}),
        ("no_native_control", {"control_offers_native_tools"}),
        ("namespaced", set()),
    ],
)
def test_each_leak_fails_the_arm_that_exists_for_it(tmp_path, leak, expected):
    (row,), _, _ = _probe(tmp_path, leak)

    assert _failed(row) == expected, row


def test_a_nested_tool_is_labelled_and_called_by_its_own_name(tmp_path):
    (row,), runs, _ = _probe(tmp_path, "namespaced")

    assert row["offered"] == ["mcp__probe/ping"]
    assert runs["candidate-0"]["ping_result"] == "pong"


def test_a_run_with_no_request_fails_rather_than_reading_as_no_tools(tmp_path):
    def silent(argv, env, cwd):
        return 0

    (row,), runs = probe_module.probe("/fake/codex", [_ALL_OFF], runner=silent, scratch=tmp_path)

    assert _failed(row) == set(REQUIRED_ARMS)
    assert runs["candidate-0"]["offered"] == []
    assert "0 requests" in row["arms"]["offers_only_server_tools"]["evidence"]


# ------------------------------------------------------------------------- labels and stand-in
def test_labels_keep_a_name_else_a_type_and_nothing_else():
    tools = [
        {"type": "function", "name": "exec_command", "parameters": {"secret": "x"}},
        {"type": "web_search", "search_context_size": "high"},
        {"type": "namespace", "name": "mcp__kc", "tools": [{"name": "read"}, {}]},
        {"parameters": {}},
        "not a tool",
    ]

    assert tool_labels(tools) == [
        "exec_command",
        "mcp__kc/read",
        "mcp__kc/unnamed",
        "unnamed",
        "web_search",
    ]
    assert tool_labels(None) == []


def test_the_stand_in_keeps_labels_outputs_and_routes_only():
    stand_in = ToolsStandIn()
    body = {
        "instructions": "not kept",
        "tools": [{"name": "mcp__probe__ping"}, {"name": "mcp__decoyproject__ping"}],
        "input": [{"type": "message", "role": "user", "content": "not kept either"}],
    }
    first = _reply_item(stand_in.answer("POST", "/v1/responses", json.dumps(body).encode())[2])
    body["input"].append({"type": "function_call_output", "call_id": "call_0", "output": "pong"})
    second = _reply_item(stand_in.answer("POST", "/v1/responses", json.dumps(body).encode())[2])
    stand_in.answer("GET", "/v1/models", b"")

    assert first["type"] == "function_call" and first["name"] == "mcp__probe__ping"
    assert second["type"] == "message"
    assert stand_in.offered == [["mcp__decoyproject__ping", "mcp__probe__ping"]] * 2
    assert stand_in.outputs == {"call_0": "pong"}
    assert stand_in.routes == ["POST /v1/responses", "POST /v1/responses", "GET /v1/models"]
    assert "not kept" not in json.dumps(vars(stand_in), default=str)


def test_the_stand_in_never_calls_a_decoy_or_a_tool_without_the_server_name():
    stand_in = ToolsStandIn()
    body = {"tools": [{"name": "mcp__decoyuser__ping"}, {"name": "ping"}], "input": []}

    item = _reply_item(stand_in.answer("POST", "/v1/responses", json.dumps(body).encode())[2])

    assert item["type"] == "message"


# --------------------------------------------------------------------------------- candidates
def test_a_candidate_splits_as_a_shell_splits_it():
    assert parse_candidate("-c 'tools.web_search=false' --disable shell_tool") == [
        "-c",
        "tools.web_search=false",
        "--disable",
        "shell_tool",
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "at least one"),
        ("   ", "at least one"),
        ("-c 'unclosed", "cannot be split"),
        ("-C /elsewhere", "-C"),
        ("--cd=/elsewhere", "--cd"),
    ],
)
def test_a_candidate_the_probe_cannot_measure_is_refused(text, message):
    with pytest.raises(ValueError, match=message):
        parse_candidate(text)


def test_a_refused_candidate_runs_nothing(tmp_path):
    fake = FakeCodex()

    with pytest.raises(ValueError, match="-C"):
        probe_module.probe("/fake/codex", [_ALL_OFF, "-C /x"], runner=fake, scratch=tmp_path)
    with pytest.raises(ValueError, match="at least one candidate"):
        probe_module.probe("/fake/codex", [], runner=fake, scratch=tmp_path)
    assert fake.calls == []


def test_the_mcp_flags_drop_the_user_config_and_quote_their_values():
    assert mcp_flags("kc", "/opt/py thon", ["-I", 'a"b']) == [
        "--ignore-user-config",
        "-c",
        'mcp_servers.kc.command="/opt/py thon"',
        "-c",
        'mcp_servers.kc.args=["-I", "a\\"b"]',
    ]


# --------------------------------------------------------------------------------- the record
def _fake_binary(path: Path, version: str) -> Path:
    path.write_text(f"#!/bin/sh\necho 'codex-cli {version}'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_record_is_keyed_by_the_binary_and_holds_every_row(tmp_path):
    rows, runs, _ = _probe(tmp_path, candidates=("-c tools.web_search=false", _ALL_OFF))
    binary = _fake_binary(tmp_path / "codex", "0.162.1")

    path = probe_module.write_record(str(binary), rows, runs)

    assert path.parent == probe_module.tools_record_dir()
    assert path.name.startswith("codex-0.162.1-")
    record = json.loads(path.read_text())
    assert record["format"] == 1
    assert record["version"] == "0.162.1"
    assert record["mcp_flags"] == mcp_flags("{server}", "{command}", ["{args}"])
    assert [row["candidate"] for row in record["rows"]] == [row["candidate"] for row in rows]
    assert set(record["runs"]) == set(runs)

    other = _fake_binary(tmp_path / "codex-other", "0.162.1")
    other.write_text(other.read_text() + "# other bytes\n")
    assert probe_module.write_record(str(other), rows, runs) != path


def test_a_binary_whose_version_cannot_be_read_gets_no_record(tmp_path):
    rows, runs, _ = _probe(tmp_path)
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 3\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(RuntimeError, match="version"):
        probe_module.write_record(str(binary), rows, runs)
    assert not probe_module.tools_record_dir().exists()


# ----------------------------------------------------------------------------------- the CLI
def test_main_without_a_candidate_or_a_codex_exits_2(monkeypatch, capsys):
    assert probe_module.main([]) == 2
    assert "at least one --candidate" in capsys.readouterr().err
    assert probe_module.main(["--candidate", "-C /x"]) == 2
    monkeypatch.setattr(probe_module.shutil, "which", lambda name: None)
    assert probe_module.main(["--candidate", _ALL_OFF]) == 2
    assert "codex is not on PATH" in capsys.readouterr().err


def _row(passed: bool, native: bool = True) -> dict:
    arms = {name: {"passed": True, "evidence": "e"} for name in REQUIRED_ARMS}
    arms["offers_only_server_tools"]["passed"] = passed
    arms["control_offers_native_tools"]["passed"] = native
    return {
        "candidate": "c",
        "settings": ["c"],
        "offered": [],
        "arms": arms,
        "passed": passed and native,
    }


@pytest.mark.parametrize(
    ("rows", "status"),
    [
        ([_row(False), _row(True)], 0),
        ([_row(False), _row(False)], 1),
        ([_row(True, native=False)], 3),
    ],
)
def test_main_exit_status_follows_the_table(monkeypatch, tmp_path, rows, status):
    monkeypatch.setattr(probe_module, "probe", lambda binary, candidates, scratch=None: (rows, {}))
    monkeypatch.setattr(probe_module, "write_record", lambda b, r, s: tmp_path / "record.json")

    argv = ["--binary", "/fake/codex"]
    for _ in rows:
        argv += ["--candidate", "-c x=1"]
    assert probe_module.main(argv) == status
