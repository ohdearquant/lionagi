"""`lion mcp` (ADR-0026 C5, D5): three tools and no other; `run` and `flow` return the child's id once it
prints it, and the child, in a session of its own, runs on; `result` reads the rows."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from lion_cli import mcp_server
from lion_cli.main import parser
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from test_submit import PROFILES, TWO, backends, lion, out

# A stand-in for `lion run` and `lion flow`: it keeps its argv and what it was started with, refuses a
# profile named `refused` on standard error as the command does, and otherwise prints an id: a `--say=slow`
# job only once a `print` file appears. A `--say=wait` job then waits for a `go` file before it writes
# `done`; a `--say=mailfail` job says on standard error that its mail failed, and exits 1; a `--say=chatty`
# job says more than the server keeps, then a last line, and exits 3. A `--say=bare` job prints its id
# with no newline; a `--say=forks` job starts a process that holds both its pipes, then exits; a
# `--say=handsoff` job prints no id and exits, and the process it started prints a line on the pipe once
# it has ended, then writes a `wrote` file.
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
if "--say=slow" in argv:  # the id only once a `print` file appears
    for _ in range(600):
        if (here / "print").exists():
            break
        time.sleep(0.05)
if "--say=bare" in argv:
    sys.stdout.write(f"{argv[0]}-id")
    sys.exit(0)
if "--say=mute" in argv:  # its standard output closed with no id, it runs on until a `go` file appears
    os.close(1)
    for _ in range(600):
        if (here / "go").exists():
            break
        time.sleep(0.05)
    sys.exit(0)
if "--say=handsoff" in argv:  # no id of its own; what comes on the pipe after it ended is another's
    import subprocess
    code = (
        f"import os, sys, time\\nwhile os.getppid() == {os.getpid()}:\\n    time.sleep(0.01)\\n"
        f"time.sleep(0.3)\\nprint('forged-id', flush=True)\\nopen({str(here / 'wrote')!r}, 'w').close()\\n"
        "time.sleep(60)\\n"
    )
    held = subprocess.Popen([sys.executable, "-c", code])
    (here / "held").write_text(str(held.pid))
    sys.exit(0)
print(f"{argv[0]}-id", flush=True)
if "--say=forks" in argv:  # the process it starts inherits both pipes and outlives it
    import subprocess
    held = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (here / "held").write_text(str(held.pid))
    sys.exit(0)
if "--say=mailfail" in argv:
    print("the result mail was not sent: KhiveError: comm down", file=sys.stderr, flush=True)
    sys.exit(1)
if "--say=chatty" in argv:
    print("x" * 20000 + "\\nthe last of it", file=sys.stderr, flush=True)
    sys.exit(3)
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


def ended(mcp, id: str, seconds: float = 30.0) -> dict:
    """`result`'s `child` for `id` once the server has seen the child end: the watcher reaps it first and
    records its exit just after, so a reaped pid alone does not say the record is there."""
    end = time.monotonic() + seconds
    while True:
        child = asyncio.run(mcp.call_tool("result", {"id": id})).structured_content["child"]
        if child != {"running": True} or time.monotonic() > end:
            return child
        time.sleep(0.05)


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


def test_a_child_that_closes_its_output_with_no_id_fails_the_call_at_once_and_is_still_reaped(
    actor_dir, child
):
    # no id can come once its standard output has ended, so the call fails then, naming the child that runs on
    here, command = child
    mcp = mcp_server.server(actor_dir, lion=command)
    began = time.monotonic()
    try:
        with pytest.raises(
            ToolError, match="lion run closed its standard output before printing an id; it runs on"
        ):
            asyncio.run(mcp.call_tool("run", {"profile": "writer", "say": "mute"}))
        assert time.monotonic() - began < 10  # far short of the child's own 30 s
        started = json.loads((here / "run.json").read_text())
        assert not gone(started["pid"], 0.2)  # still running
    finally:
        (here / "go").write_text("")
    assert gone(started["pid"])  # and reaped once it ended


def test_calls_whose_children_close_their_output_fail_in_about_a_second_with_the_shared_pool_held(
    actor_dir, child
):
    # the wait for each child is the loop's own, so a pool of threads every call shares, held here by
    # another job for 4 s, delays none of them
    here, command = child
    mcp = mcp_server.server(actor_dir, lion=command)

    async def main() -> tuple[list, float]:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        held = loop.run_in_executor(None, time.sleep, 4)
        began = time.monotonic()
        calls = (mcp.call_tool("run", {"profile": "writer", "say": "mute"}) for _ in range(3))
        failed = await asyncio.gather(*calls, return_exceptions=True)
        took = time.monotonic() - began
        await held
        return failed, took

    try:
        failed, took = asyncio.run(main())
    finally:
        (here / "go").write_text("")
    assert all(isinstance(e, ToolError) and "it runs on as pid" in str(e) for e in failed)
    assert took < 3.5  # each about a second; behind the held pool the first alone would take 5
    for e in failed:
        assert gone(int(re.search(r"pid (\d+)", str(e)).group(1)))


def test_a_child_no_thread_can_read_fails_the_call_naming_it_and_the_loop_reaps_it(
    actor_dir, child, monkeypatch
):
    here, command = child
    mcp = mcp_server.server(actor_dir, lion=command)
    start = threading.Thread.start

    def refused(self):
        if self._target is mcp_server._watch:
            raise RuntimeError("can't start new thread")
        start(self)

    monkeypatch.setattr(threading.Thread, "start", refused)

    async def main() -> bool:
        with pytest.raises(ToolError, match=r"lion run started and could not be read \(can't start new") as e:
            await mcp.call_tool("run", {"profile": "writer", "say": "mute"})
        pid = int(re.search(r"it runs on as pid (\d+)", str(e.value)).group(1))
        assert waited(here / "run.json") and json.loads((here / "run.json").read_text())["pid"] == pid
        (here / "go").write_text("")
        for _ in range(600):  # the server's loop goes on, as it does under a client, and reaps it
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            await asyncio.sleep(0.05)
        return False

    assert asyncio.run(main())


def test_a_child_no_thread_can_reap_still_gives_its_id_and_is_reaped_when_its_pipes_end(
    actor_dir, console_khive, child, monkeypatch
):
    here, command = child
    console_khive.answer(
        'lion.session_open(id="run-id", source="lion1", actor="console", profile="writer") | '
        'lion.run_open(id="run-id", profile="writer", session_id="run-id", actor="console")'
    )
    mcp = mcp_server.server(actor_dir, lion=command)
    start = threading.Thread.start

    def refused(self):
        if getattr(self._target, "__name__", "") == "reaped":
            raise RuntimeError("can't start new thread")
        start(self)

    monkeypatch.setattr(threading.Thread, "start", refused)
    fds = len(os.listdir("/dev/fd"))
    call = mcp.call_tool("run", {"profile": "writer", "say": "wait"})
    got = asyncio.run(asyncio.wait_for(call, 30))
    assert got.structured_content == {"id": "run-id"}
    pid = json.loads((here / "run.json").read_text())["pid"]
    (here / "go").write_text("")
    assert ended(mcp, "run-id") == {"exit": 0, "said": ""} and gone(pid)
    assert len(os.listdir("/dev/fd")) == fds  # the pipe its reaping would have woken the reader with: closed


def test_a_call_cancelled_before_the_id_still_has_its_child_reaped(actor_dir, child):
    here, command = child
    mcp = mcp_server.server(actor_dir, lion=command)

    async def cancelled() -> None:
        call = asyncio.ensure_future(mcp.call_tool("run", {"profile": "writer", "say": "slow"}))
        for _ in range(1500):  # the child has started, and waits for `print`
            if (here / "run.json").exists():
                break
            await asyncio.sleep(0.02)
        assert (here / "run.json").exists()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(cancelled())  # the loop the call waited on is gone before the child prints
    started = json.loads((here / "run.json").read_text())
    (here / "print").write_text("")
    assert gone(started["pid"])  # the child printed its id to no one, ended, and was reaped


def test_the_result_tool_tells_how_a_child_it_started_ended_a_failed_mail_included(
    actor_dir, console_khive, child
):
    here, command = child
    for name in ("run-id", "flow-id"):  # the rows a real child writes before it prints the id
        console_khive.answer(
            f'lion.session_open(id="{name}", source="lion1", actor="console", profile="writer") | '
            f'lion.run_open(id="{name}", profile="writer", session_id="{name}", actor="console")'
        )
    mcp = mcp_server.server(actor_dir, lion=command)

    assert asyncio.run(mcp.call_tool("run", {"profile": "writer", "say": "mailfail"})).structured_content == {
        "id": "run-id"
    }
    said = "the result mail was not sent: KhiveError: comm down\n"
    assert ended(mcp, "run-id") == {"exit": 1, "said": said}
    assert gone(json.loads((here / "run.json").read_text())["pid"])
    assert asyncio.run(mcp.call_tool("flow", {"name": "two"})).structured_content == {"id": "flow-id"}
    assert ended(mcp, "flow-id") == {"exit": 0, "said": ""}
    # a run of another's, or one before this server, carries no `child`
    console_khive.answer(
        'lion.session_open(id="other", source="lion1", actor="console", profile="writer") | '
        'lion.run_open(id="other", profile="writer", session_id="other", actor="console")'
    )
    assert "child" not in asyncio.run(mcp.call_tool("result", {"id": "other"})).structured_content


def test_the_result_tool_says_a_child_it_started_is_still_running(actor_dir, console_khive, child):
    here, command = child
    console_khive.answer(
        'lion.session_open(id="run-id", source="lion1", actor="console", profile="writer") | '
        'lion.run_open(id="run-id", profile="writer", session_id="run-id", actor="console")'
    )
    mcp = mcp_server.server(actor_dir, lion=command)
    assert asyncio.run(mcp.call_tool("run", {"profile": "writer", "say": "wait"})).structured_content == {
        "id": "run-id"
    }
    got = asyncio.run(mcp.call_tool("result", {"id": "run-id"})).structured_content
    assert got["child"] == {"running": True}
    (here / "go").write_text("")
    assert ended(mcp, "run-id") == {"exit": 0, "said": ""}
    assert gone(json.loads((here / "run.json").read_text())["pid"])


def test_the_result_tool_keeps_the_last_of_what_a_child_said(actor_dir, console_khive, child):
    console_khive.answer(
        'lion.session_open(id="run-id", source="lion1", actor="console", profile="writer") | '
        'lion.run_open(id="run-id", profile="writer", session_id="run-id", actor="console")'
    )
    mcp = mcp_server.server(actor_dir, lion=child[1])
    assert asyncio.run(mcp.call_tool("run", {"profile": "writer", "say": "chatty"})).structured_content == {
        "id": "run-id"
    }
    got = ended(mcp, "run-id")
    assert got["exit"] == 3 and got["said"].endswith("x\nthe last of it\n")
    assert len(got["said"]) == mcp_server._SAID


def test_a_child_that_ends_while_a_process_it_started_holds_its_pipes_is_reaped_and_told_ended(
    actor_dir, console_khive, child
):
    here, command = child
    console_khive.answer(
        'lion.session_open(id="run-id", source="lion1", actor="console", profile="writer") | '
        'lion.run_open(id="run-id", profile="writer", session_id="run-id", actor="console")'
    )
    mcp = mcp_server.server(actor_dir, lion=command)
    assert asyncio.run(mcp.call_tool("run", {"profile": "writer", "say": "forks"})).structured_content == {
        "id": "run-id"
    }
    assert waited(here / "held")
    held = int((here / "held").read_text())
    try:
        started = time.monotonic()
        assert ended(mcp, "run-id") == {"exit": 0, "said": ""}
        assert time.monotonic() - started < 10  # not when the held pipes end, a minute on
        assert gone(json.loads((here / "run.json").read_text())["pid"])
    finally:
        os.kill(held, signal.SIGKILL)


def test_an_id_the_child_wrote_with_no_newline_before_it_ended_is_its_id(actor_dir, child):
    _, command = child
    mcp = mcp_server.server(actor_dir, lion=command)
    assert asyncio.run(mcp.call_tool("run", {"profile": "writer", "say": "bare"})).structured_content == {
        "id": "run-id"
    }


def test_a_line_a_process_the_child_started_prints_after_the_child_ended_is_not_its_id(actor_dir, child):
    # what the pipe holds is measured just after the reap, so a line the process wrote in the moment
    # between them would still count as the id, and so would one it wrote before the child ended: a
    # pipe does not say which process wrote to it. This one writes 0.3 s after the child has ended
    here, command = child
    mcp = mcp_server.server(actor_dir, lion=command)
    try:
        with pytest.raises(ToolError, match="lion run ended with 0 before printing an id"):
            asyncio.run(mcp.call_tool("run", {"profile": "writer", "say": "handsoff"}))
        assert waited(here / "wrote")  # its line went on the pipe while the server still read it
    finally:
        if waited(here / "held"):
            os.kill(int((here / "held").read_text()), signal.SIGKILL)


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
