"""`lion mcp` (ADR-0026 C5, D5): three tools and no other; `run` and `flow` return the child's id once it
prints it, and the child, in a session of its own, runs on; `result` reads the rows."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest
from lion_cli import mcp_server
from lion_cli.main import parser
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from test_submit import PROFILES, TWO, backends, lion, out

# A stand-in for `lion run` and `lion flow`: it keeps its argv and what it was started with, refuses a
# profile named `refused` on standard error as the command does, and otherwise prints an id; a `--say=wait`
# job then waits for a `go` file before it writes `done`.
CHILD = """
import json, os, sys, time
from pathlib import Path
here, argv = Path(sys.argv[1]), sys.argv[2:]
null = os.stat(os.devnull)
(here / f"{argv[0]}.json").write_text(json.dumps({
    "argv": argv, "pid": os.getpid(), "own_session": os.getsid(0) == os.getpid(),
    "stdin_null": os.path.samestat(os.fstat(0), null),
}))
if "--profile=refused" in argv:
    print("lion run: profiles.toml has no [profile.refused]", file=sys.stderr, flush=True)
    sys.exit(1)
print(f"{argv[0]}-id", flush=True)
if "--say=wait" in argv:
    for _ in range(600):
        if (here / "go").exists():
            break
        time.sleep(0.05)
    (here / "done").write_text("ran on")
"""


@pytest.fixture
def actor_dir(tmp_path, console_khive):
    (tmp_path / "profiles.toml").write_text(PROFILES)
    (tmp_path / "mailer.md").write_text("You report.")
    (tmp_path / "flows.toml").write_text(TWO)
    return tmp_path


@pytest.fixture
def child(tmp_path_factory):
    """The stand-in's directory, and the `lion` command that runs it."""
    here = tmp_path_factory.mktemp("child")
    (here / "child.py").write_text(CHILD)
    return here, (sys.executable, str(here / "child.py"), str(here))


def gone(pid: int, seconds: float = 30.0) -> bool:
    """The process has ended and been reaped: its pid no longer names one."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def waited(path, seconds: float = 30.0) -> bool:
    end = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > end:
            return False
        time.sleep(0.05)
    return True


def test_the_mcp_server_lists_run_flow_and_result_and_no_other_tool(actor_dir):
    tools = asyncio.run(mcp_server.server(actor_dir).list_tools())
    assert [t.name for t in tools] == ["run", "flow", "result"]
    given = {t.name: set(t.input_schema["properties"]) for t in tools}
    # the model is the profile's: no tool takes one from the caller
    assert given == {
        "run": {"profile", "say", "files", "variables", "thread"},
        "flow": {"name", "variables", "thread"},
        "result": {"id", "history"},
    }


def test_the_mcp_run_tool_returns_the_id_once_printed_and_the_child_runs_on(actor_dir, console_khive, child):
    here, command = child
    before = sorted(p.name for p in actor_dir.iterdir())
    mcp = mcp_server.server(actor_dir, lion=command)

    async def call() -> dict:
        got = await mcp.call_tool(
            "run",
            {"profile": "writer", "say": "wait", "files": ["a.txt"], "variables": {"x": "1"}, "thread": "t1"},
        )
        return got.structured_content

    # the server's own standard input is a pipe here, as it is under a client, so a child that took it
    # would not read as the null device
    saved, (r, w) = os.dup(0), os.pipe()
    os.dup2(r, 0)
    try:
        assert asyncio.run(call()) == {"id": "run-id"}
    finally:
        os.dup2(saved, 0)
        for fd in (saved, r, w):
            os.close(fd)
    started = json.loads((here / "run.json").read_text())
    assert not (here / "done").exists()  # returned while the child still waits
    assert started["own_session"] and started["stdin_null"]
    assert started["argv"] == [
        "run",
        "--profile=writer",
        "--say=wait",
        "--file=a.txt",
        "--var=x=1",
        "--thread=t1",
        f"--dir={actor_dir}",
        "--notify=console",  # the server's actor is the submitter
    ]
    (here / "go").write_text("")
    assert waited(here / "done")  # the server stopped reading long before: the child ran on
    assert gone(started["pid"])  # and the server reaped it
    assert asyncio.run(mcp.call_tool("flow", {"name": "two"})).structured_content == {"id": "flow-id"}
    flowed = json.loads((here / "flow.json").read_text())
    assert flowed["argv"] == ["flow", "--name=two", f"--dir={actor_dir}", "--notify=console"]
    assert gone(flowed["pid"])
    # the tools themselves mail no one and write nothing in the directory
    assert console_khive.sent == [] and sorted(p.name for p in actor_dir.iterdir()) == before


def test_a_child_that_ends_before_its_id_fails_the_call_with_what_it_said(actor_dir, child):
    _, command = child
    mcp = mcp_server.server(actor_dir, lion=command)
    with pytest.raises(ToolError, match=r"lion run: profiles.toml has no \[profile.refused\]"):
        asyncio.run(mcp.call_tool("run", {"profile": "refused", "say": "go"}))


def test_a_value_that_starts_with_a_dash_reaches_the_command_as_a_value(actor_dir):
    a = parser().parse_args(
        ["run", "--profile=writer", "--say=--help me", "--var=v=--x", f"--dir={actor_dir}"]
    )
    assert (a.say, a.var, a.dir) == ("--help me", ["v=--x"], str(actor_dir))


def test_the_mcp_result_tool_returns_the_row_a_flows_steps_and_with_history_the_messages(
    actor_dir, console_khive, monkeypatch
):
    backends(monkeypatch, **{"fake/writer": lambda m: out("read back")})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go on") == 0
    (run_id,) = console_khive.runs
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 0
    (flow_id,) = console_khive.steps
    mcp = mcp_server.server(actor_dir)

    async def result(**args) -> dict:
        return (await mcp.call_tool("result", args)).structured_content

    got = asyncio.run(result(id=run_id))
    assert got == {"row": console_khive.runs[run_id]}
    history = asyncio.run(result(id=run_id, history=True))["messages"]
    assert len(history) == len(console_khive.messages[run_id]) and history[0]["text"] == "go on"
    flowed = asyncio.run(result(id=flow_id))
    assert [s["name"] for s in flowed["steps"]] == ["plan", "write"]
    with pytest.raises(ToolError, match="not in this namespace"):
        asyncio.run(result(id="nope"))


def test_lion_mcp_serves_the_directory_on_standard_io(actor_dir, monkeypatch):
    served = []

    async def run_stdio_async(self):
        served.append(sorted(t.name for t in await self.list_tools()))

    monkeypatch.setattr(MCPServer, "run_stdio_async", run_stdio_async)
    assert lion("mcp", "--dir", str(actor_dir)) == 0
    assert served == [["flow", "result", "run"]]


def test_lion_mcp_refuses_a_directory_with_no_actor_and_a_missing_mcp_package(tmp_path, monkeypatch):
    assert "no actor" in str(lion("mcp", "--dir", str(tmp_path)))
    monkeypatch.delitem(sys.modules, "lion_cli.mcp_server")
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    assert (
        lion("mcp", "--dir", str(tmp_path))
        == "lion mcp: the mcp package is not installed; install lion-cli[mcp]"
    )
