"""The shell tool: `bash` and `python` handlers over a sandbox. The handlers are tested on a fake
sandbox (what argv and stdin they hand it, the result shape); the real sandbox, an Apple `container`
VM, is exercised once end to end with `LION_CONTAINER_E2E=1` on a machine with that CLI: mount,
environment, exit code, timeout, no host access, stop."""

import asyncio
import os
import shutil
import time

import pytest

from hub import Sandbox, files, shell
from lion1 import Actor, Kind, Profile, RunRequest
from lion1.actor import DEFAULT_HANDLERS
from tests.test_actor import scripted


class FakeSandbox:
    def __init__(self):
        self.calls = []

    async def exec(self, argv, *, stdin=None, timeout=120):
        self.calls.append((argv, stdin, timeout))
        return {"rc": 0, "stdout": "out", "stderr": "", "elapsed": 0.01}


def test_the_handlers_hand_the_sandbox_a_shell_line_and_a_script_on_stdin(store):
    actor = Actor("s", store=store)
    box = FakeSandbox()
    assert shell(actor, box) == {"bash", "python"}
    profile = Profile(
        name="s", specs=frozenset({"bash", "python"} | {h.name for h in DEFAULT_HANDLERS}), system="x"
    )
    backend = scripted(
        '<lact b1>bash(command="ls -la", timeout=7)</lact><lact p1>python(code="print(1)")</lact>', "ok"
    )
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_turns=2), backend))
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["b1"].ok and r["b1"].value["stdout"] == "out" and r["p1"].ok
    by_stdin = {c[1]: c for c in box.calls}
    assert by_stdin[None][0] == ["bash", "-o", "pipefail", "-c", "ls -la"] and by_stdin[None][2] == 7
    assert by_stdin[b"print(1)"][0][-1].endswith("exec $P -") and by_stdin[b"print(1)"][2] == 120


@pytest.mark.skipif(
    os.environ.get("LION_CONTAINER_E2E") != "1" or shutil.which("container") is None,
    reason="LION_CONTAINER_E2E=1 boots a real VM with Apple's container CLI",
)
def test_a_real_sandbox_mounts_the_directory_runs_python_ends_a_hung_command_and_stops(tmp_path, store):
    (tmp_path / "a.txt").write_text("alpha\n")
    box = Sandbox(tmp_path, f"lion-test-{int(time.time())}")
    actor = Actor("s", store=store)
    specs = shell(actor, box) | files(actor, tmp_path) | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="s", specs=frozenset(specs), system="x")

    async def go():
        assert await box.ensure_image() is None
        await box.start()
        try:
            probe = "cat a.txt; echo made > b.txt; pwd; id -u; ls ~/.ssh 2>&1 | head -1; "
            probe += "date +%Z; exit 3"
            backend = scripted(
                f'<lact b1>bash(command="{probe}")</lact>'
                '<lact p1>python(code="import sys; print(sys.version_info[:2], sys.platform)")</lact>'
                '<lact t1>bash(command="sleep 30", timeout=1)</lact>',
                "ok",
            )
            return await actor.run(profile, RunRequest(inputs=("go",), max_turns=2), backend)
        finally:
            await box.stop()

    run = asyncio.run(go())
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    b1 = r["b1"].value
    assert b1["rc"] == 3 and b1["stdout"].startswith(f"alpha\n{tmp_path.resolve()}\n")
    assert "No such file" in b1["stdout"] or "cannot access" in b1["stdout"]  # the host's home is not there
    assert (tmp_path / "b.txt").read_text() == "made\n"  # the mount is the real directory
    assert b1["stdout"].splitlines()[-1] == time.strftime("%Z")  # the sandbox keeps the host's zone
    assert r["p1"].value["rc"] == 0 and "linux" in r["p1"].value["stdout"]
    assert r["t1"].value["rc"] == 137 and "ended at the 1s timeout" in r["t1"].value["stderr"]
    assert r["t1"].value["elapsed"] < 10
    assert not box.started


def test_every_real_box_test_is_opt_in():
    # a test that boots a real VM or box is
    # skipped unless its opt-in variable is set (the original asserted both variables unset instead)
    import tests.test_daytona as daytona
    import tests.test_runta as runta

    for var, fn in (
        ("LION_DAYTONA_E2E", daytona.test_a_real_box_runs_the_shell_and_code_tools_over_a_pushed_directory),
        (
            "LION_CONTAINER_E2E",
            test_a_real_sandbox_mounts_the_directory_runs_python_ends_a_hung_command_and_stops,
        ),
        ("LION_RUNTA_E2E", runta.test_a_real_runtime_runs_the_shell_and_code_tools_proves_egress_and_stops),
    ):
        (mark,) = [m for m in fn.pytestmark if m.name == "skipif"]
        assert os.environ.get(var) == "1" or mark.args[0], f"{fn.__name__} runs without {var}=1"


@pytest.mark.parametrize(("cut_at", "stops"), [("volume", False), ("run", True)])
def test_a_sandbox_whose_start_was_cut_stops_a_container_once_its_run_was_sent(tmp_path, cut_at, stops):
    calls = []

    class Cut(Sandbox):
        async def _cli(self, *argv, stdin=None, timeout=None):
            calls.append(argv[0])
            if argv[0] == cut_at:
                raise asyncio.CancelledError  # an interrupt landing in this command
            return 0, "", ""

    box = Cut(tmp_path, "lion-test-cut")

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await box.start()
        assert not box.started and box.launched is stops
        calls.clear()
        await box.stop()

    asyncio.run(go())
    assert calls == (["stop", "delete"] if stops else [])
    assert not box.launched and not box.started


def test_a_sandbox_whose_run_was_cut_before_it_was_sent_has_nothing_to_stop(tmp_path, monkeypatch):
    calls = []

    class Quiet(Sandbox):
        async def _cli(self, *argv, stdin=None, timeout=None):
            calls.append(argv[0])
            return 0, "", ""

    made = asyncio.ensure_future

    def cut_before_it_starts(aw):  # as the loop's end does at a second interrupt: every task cancelled
        task = made(aw)
        task.cancel()
        return task

    box = Quiet(tmp_path, "lion-test-unsent")
    monkeypatch.setattr(asyncio, "ensure_future", cut_before_it_starts)

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await box.start()
        assert not box.launched and not box.started  # its `run` was never sent
        await box.stop()

    asyncio.run(go())
    assert "run" not in calls and "stop" not in calls and "delete" not in calls


@pytest.mark.parametrize(
    ("listed", "raises"),
    [
        ((0, '[{"configuration": {"id": "NAME"}}]'), True),  # still there: the stop failed
        ((0, "[]"), False),  # never made: a `run` cut before it made one leaves nothing to delete
        ((0, '[{"configuration": {"id": "NAME-2"}}]'), False),  # another, whose name begins with this one
        ((1, ""), True),  # the list cannot be read: whether it runs is not known
    ],
)
def test_a_stop_whose_delete_fails_raises_while_its_container_may_still_be_there(tmp_path, listed, raises):
    calls = []

    class Stuck(Sandbox):
        async def _cli(self, *argv, stdin=None, timeout=None):
            calls.append(argv[0])
            if argv[0] == "delete":
                return 1, "", "made-up: container is busy"
            if argv[0] == "list":
                return listed[0], listed[1].replace("NAME", self.name), ""
            return 1 if argv[0] == "stop" else 0, "", ""

    box = Stuck(tmp_path, "lion-test-stuck", launched=True)

    async def go():
        if raises:
            with pytest.raises(RuntimeError, match="sandbox lion-test-stuck: its delete exited 1: made-up"):
                await box.stop()
            assert box.launched  # a later stop tries again
        else:
            await box.stop()
            assert not box.launched and not box.started

    asyncio.run(go())
    assert calls == ["stop", "delete", "list"]


def test_a_first_interrupt_in_the_containers_run_lets_it_answer_before_the_stop(tmp_path):
    calls = []

    class Slow(Sandbox):
        async def _cli(self, *argv, stdin=None, timeout=None):
            calls.append(argv[0])
            if argv[0] == "run":
                await asyncio.sleep(0.1)
                calls.append("run answered")
            return 0, "", ""

    box = Slow(tmp_path, "lion-test-slow")

    async def go():
        start = asyncio.ensure_future(box.start())
        while "run" not in calls:
            await asyncio.sleep(0.005)
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        await box.stop()

    asyncio.run(go())
    assert calls[-4:] == ["run", "run answered", "stop", "delete"]
