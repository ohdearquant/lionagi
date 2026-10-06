"""The shell tool: `bash` and `python` handlers over a sandbox. The handlers are tested on a fake
sandbox (what argv and stdin they hand it, the result shape); the real sandbox, an Apple `container`
VM, is exercised end to end with `LION_CONTAINER_E2E=1` on a machine with that CLI: mount,
environment, exit code, timeout, the host's home absent, stop, and the offline network."""

import asyncio
import http.server
import json
import os
import shutil
import threading
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
        try:  # a start that fails after its `run` was sent still has a VM to stop
            await box.start()
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


# from inside the VM: its gateway, whether a name resolves, whether a public address takes a connection,
# and what the host's server answers at the gateway
_NETWORK_PROBE = """
import json, socket, struct, urllib.request
routes = [line.split() for line in open("/proc/net/route").read().splitlines()[1:]]
gw = next(socket.inet_ntoa(struct.pack("<L", int(f[2], 16))) for f in routes if f[1] == "00000000")
out = {}
try:
    socket.getaddrinfo("example.com", 80)
    out["dns"] = True
except OSError:
    out["dns"] = False
try:
    socket.create_connection(("1.1.1.1", 80), timeout=5).close()
    out["egress"] = True
except OSError:
    out["egress"] = False
try:
    out["host"] = urllib.request.urlopen(f"http://{gw}:PORT/", timeout=5).read().decode()
except OSError as e:
    out["host"] = repr(e)
print(json.dumps(out))
"""


@pytest.mark.skipif(
    os.environ.get("LION_CONTAINER_E2E") != "1" or shutil.which("container") is None,
    reason="LION_CONTAINER_E2E=1 boots a real VM with Apple's container CLI",
)
def test_an_offline_sandbox_has_no_dns_and_no_egress_and_still_reaches_the_host(tmp_path):
    # `--offline` is a host-only network (ADR-0011 S20): a server the host runs on all its addresses
    # answers the VM at the network's gateway. The same probe on the default network is the control
    # that makes the two refusals mean something: there the name resolves and the address connects
    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"the host")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    probe = _NETWORK_PROBE.replace("PORT", str(server.server_address[1])).encode()

    async def seen(offline: bool) -> dict:
        box = Sandbox(tmp_path, f"lion-test-net-{int(offline)}-{int(time.time())}", offline=offline)
        assert await box.ensure_image() is None
        try:
            await box.start()
            got = await box.exec(["python3", "-"], stdin=probe, timeout=60)
        finally:
            await box.stop()
        assert got["rc"] == 0, got
        return json.loads(got["stdout"])

    try:
        online, offline = asyncio.run(seen(False)), asyncio.run(seen(True))
    finally:
        server.shutdown()
        server.server_close()
    assert online == {"dns": True, "egress": True, "host": "the host"}
    assert offline == {"dns": False, "egress": False, "host": "the host"}


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
        ("LION_CONTAINER_E2E", test_an_offline_sandbox_has_no_dns_and_no_egress_and_still_reaches_the_host),
        ("LION_RUNTA_E2E", runta.test_a_real_runtime_runs_the_shell_and_code_tools_proves_egress_and_stops),
    ):
        (mark,) = [m for m in fn.pytestmark if m.name == "skipif"]
        assert os.environ.get(var) == "1" or mark.args[0], f"{fn.__name__} runs without {var}=1"


def test_the_tool_descriptions_every_box_shares_claim_nothing_only_the_vm_has():
    # `lion chat --box daytona` registers the same Bash and Python models as the VM and appends the same
    # coding guidance, so what only the VM supplies (the mount, the /opt volume, each of its tools) is said
    # in the VM's own guidance; the Daytona guidance names the tools that work on the copy, not every tool
    import inspect
    from pathlib import Path

    from hub.tools.shell import Bash, Python

    guidance = Path(inspect.getfile(Bash)).parent.parent / "guidance"
    said = " ".join(
        [
            Bash.__doc__,
            Python.__doc__,
            *(f.description or "" for m in (Bash, Python) for f in m.model_fields.values()),
            (guidance / "code.md").read_text(),
            (guidance / "code-noshell.md").read_text(),
        ]
    )
    vm = (guidance / "sandbox.md").read_text()
    only = ("VM", "mounted", "volume", "kept from one conversation", "no other host path")
    works = ("`uv run ...`", "`uv sync`")  # what the VM's environment lets a command do
    for vm_only in (*only, *works, "`uv`", "`git`", "`rg`", "`jq`"):
        assert vm_only not in said, vm_only
        assert vm_only in vm, vm_only
    assert "every tool" not in (guidance / "box.md").read_text()


HOST_ONLY = json.dumps(
    [{"id": "lion-offline", "configuration": {"mode": "hostOnly", "name": "lion-offline"}}]
)
NAT = json.dumps([{"id": "lion-offline", "configuration": {"mode": "nat"}}])


@pytest.mark.parametrize(
    ("inspected", "creates", "starts"),
    [
        ([(0, HOST_ONLY)], False, True),  # there and host-only: attached as it is
        ([(1, ""), (0, HOST_ONLY)], True, True),  # missing: made with --internal, then read back
        ([(0, "[]"), (0, HOST_ONLY)], True, True),
        ([(0, NAT)], False, False),  # one of that name with egress: never attached
        ([(1, ""), (0, NAT)], True, False),  # nor one the create did not make host-only
        ([(1, ""), (1, "")], True, False),  # nor one that cannot be read back
        (
            [(0, json.dumps([{"id": "lion-offline-2", "configuration": {"mode": "hostOnly"}}]))] * 2,
            True,
            False,
        ),
        ([(0, json.dumps([{"id": "lion-offline", "configuration": "hostOnly"}]))], False, False),
        ([(0, json.dumps([{"id": "lion-offline"}]))], False, False),
        ([(0, "7"), (0, '{"id": "lion-offline"}')], True, False),  # JSON that lists no network
        ([(1, HOST_ONLY), (1, HOST_ONLY)], True, False),  # an inspect that failed found nothing
        ([(0, json.dumps(json.loads(HOST_ONLY) + json.loads(NAT)))], False, False),  # two of the name
        ([(0, json.dumps(json.loads(NAT) + json.loads(HOST_ONLY)))], False, False),
        ([(0, HOST_ONLY[1:-1]), (0, HOST_ONLY[1:-1])], True, False),  # the network alone, in no list
    ],
)
def test_an_offline_sandbox_attaches_only_a_network_the_cli_says_is_host_only(
    tmp_path, inspected, creates, starts
):
    calls, answers = [], list(inspected)

    class Fake(Sandbox):
        async def _cli(self, *argv, stdin=None, timeout=None):
            calls.append(argv)
            if argv[:2] == ("network", "inspect"):
                rc, out = answers.pop(0)
                return rc, out, ""
            return 0, "", ""

    box = Fake(tmp_path, "lion-test-net", offline=True)
    if starts:
        asyncio.run(box.start())
        (run,) = [c for c in calls if c[0] == "run"]
        assert run[run.index("--network") + 1] == "lion-offline"
    else:
        with pytest.raises(RuntimeError, match="the network lion-offline is not host-only"):
            asyncio.run(box.start())
        assert not [c for c in calls if c[0] == "run"] and not box.launched
    made = [c for c in calls if c[:2] == ("network", "create")]
    assert made == ([("network", "create", "--internal", "lion-offline")] if creates else [])
    assert all(c == ("network", "inspect", "lion-offline") for c in calls if c[:2] == ("network", "inspect"))


def test_a_sandbox_that_is_not_offline_asks_nothing_of_the_network(tmp_path):
    calls = []

    class Fake(Sandbox):
        async def _cli(self, *argv, stdin=None, timeout=None):
            calls.append(argv)
            return 0, "", ""

    asyncio.run(Fake(tmp_path, "lion-test-net").start())
    assert not [c for c in calls if c[0] == "network"] and "--network" not in calls[-1]


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
