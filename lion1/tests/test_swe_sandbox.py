"""The bench's box is the hub's: `testbed` picks docker or Daytona over the eval image, with the
testbed environment as the prelude and /testbed as the directory; a box that never comes up is
replaced and a box that dies is named (through the hub's own start logic)."""

import asyncio
import sys
import types
from pathlib import Path

import pytest

from hub.tools import DaytonaSandbox, DockerSandbox, SandboxDead
from hub.tools import daytona as daytona_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "swe"))
import sandbox  # noqa: E402

NO_IP = "Failed to execute command: bad request: failed to resolve container IP after 3 attempts"


def test_testbed_makes_the_hub_box_over_the_eval_image():
    local = sandbox.testbed("img", "local")
    assert isinstance(local, DockerSandbox) and (local.workdir, local.prelude) == (
        "/testbed",
        sandbox.PRELUDE,
    )
    remote = sandbox.testbed("img", "daytona", cpu=4, memory=8, label="lionagi-swe-x")
    assert (
        isinstance(remote, DaytonaSandbox)
        and remote.workdir == "/testbed"
        and remote.prelude == sandbox.PRELUDE
    )
    assert (remote.cpu, remote.memory, remote.label) == (4, 8, "lionagi-swe-x")


def stub_daytona(created: list, deleted: list) -> types.ModuleType:
    class DaytonaError(Exception):
        pass

    class Timeout(Exception):
        pass

    class Proc:
        def __init__(self, ok: bool):
            self.ok = ok

        async def exec(self, cmd, cwd=None, env=None, timeout=None):
            if not self.ok:
                raise DaytonaError(NO_IP)
            return types.SimpleNamespace(exit_code=0, result="")

    class Box:
        def __init__(self, ok: bool):
            self.process = Proc(ok)

        async def delete(self):
            deleted.append(self)

    class Client:
        def __init__(self, cfg):
            pass

        async def create(self, params, timeout=None):
            box = Box(ok=len(created) >= 1)  # the first box never comes up, the second does
            created.append(box)
            return box

        async def close(self):
            pass

    mod = types.ModuleType("daytona")
    mod.AsyncDaytona = Client
    mod.DaytonaConfig = lambda api_key: None
    mod.CreateSandboxFromImageParams = lambda **kw: kw
    mod.Resources = lambda **kw: kw
    mod.DaytonaError = DaytonaError
    mod.DaytonaProcessExecutionTimeoutError = Timeout
    return mod


def test_a_box_that_never_comes_up_is_replaced_and_a_box_that_dies_is_named(monkeypatch):
    created: list = []
    deleted: list = []
    monkeypatch.setitem(sys.modules, "daytona", stub_daytona(created, deleted))
    monkeypatch.setattr(daytona_mod, "api_key", lambda: "k")
    box = asyncio.run(sandbox.testbed("img", "daytona").start())
    assert len(created) == 2 and deleted == [created[0]] and box._box is created[1] and box.dead is None
    assert asyncio.run(box.run("true")) == (0, "")
    created[1].process.ok = False
    with pytest.raises(SandboxDead, match="resolve container IP"):
        asyncio.run(box.run("true"))
    assert "resolve container IP" in box.dead  # the harness reads this to end the run


class ScriptedBox:
    """A box whose `run` answers from a script: the output per command, the first match by prefix."""

    workdir, prelude, dead = "/testbed", "", None

    def __init__(self, answers: dict[str, tuple[int, str]]):
        self.answers, self.ran = answers, []

    async def run(self, cmd, *, timeout=120, env=None):
        self.ran.append(cmd)
        for head, r in self.answers.items():
            if cmd.startswith(head):
                return r
        return 0, ""


def test_the_unaided_probe_reads_dns_and_the_fetch_apart_and_a_reachable_host_is_a_refusal():
    # `127.0.0.1 github.com` beside an HTTP 200 (a proxy in the box's environment) is aided
    hosts = {"printf": (0, "127.0.0.1 github.com # lion-unaided\n127.0.0.1 pypi.org # lion-unaided\n")}
    ours = (
        "127.0.0.1     STREAM {h}\n127.0.0.1     DGRAM  \n::1           STREAM {h}\n::1           DGRAM  \n"
    )
    pypi_closed = {"getent ahosts pypi.org": (0, ours.format(h="pypi.org") + "\n000 curl=7\n")}
    gh = "getent ahosts github.com"
    closed = ScriptedBox({**hosts, **pypi_closed, gh: (0, ours.format(h="github.com") + "\n000 curl=7\n")})
    assert asyncio.run(sandbox.close_code_hosts(closed)) == (
        "code hosts and package indexes closed; github.com HTTP 000 curl rc 7, pypi.org HTTP 000 curl rc 7"
    )
    assert closed.ran[0].startswith("printf") and "pypi.org # lion-unaided" in closed.ran[0]
    assert "::1 pypi.org # lion-unaided" in closed.ran[0]  # the v6 family is closed with the v4 one
    timed = ScriptedBox({**hosts, **pypi_closed, gh: (0, ours.format(h="github.com") + "\n000 curl=28\n")})
    assert "github.com HTTP 000 curl rc 28" in asyncio.run(sandbox.close_code_hosts(timed))
    proxied = ScriptedBox({**hosts, **pypi_closed, gh: (0, ours.format(h="github.com") + "\n200 curl=0\n")})
    with pytest.raises(RuntimeError, match="github.com is reachable.*HTTP 200"):
        asyncio.run(sandbox.close_code_hosts(proxied))
    elsewhere = ScriptedBox(
        {**hosts, **pypi_closed, gh: (0, "140.82.112.3 STREAM github.com\n\n000 curl=7\n")}
    )
    with pytest.raises(RuntimeError, match="still resolves elsewhere"):
        asyncio.run(sandbox.close_code_hosts(elsewhere))
    # `getent hosts` answered pypi.org with DNS's AAAA record beside the v4 closure;
    # every address, both families, must be ours, and a v6 one from DNS is elsewhere
    v6 = {
        "getent ahosts pypi.org": (
            0,
            "127.0.0.1 STREAM pypi.org\n2a04:4e42::223 STREAM pypi.org\n\n000 curl=7\n",
        )
    }
    with pytest.raises(RuntimeError, match="pypi.org still resolves elsewhere: .*2a04:4e42::223"):
        asyncio.run(
            sandbox.close_code_hosts(
                ScriptedBox({**hosts, **v6, gh: (0, ours.format(h="github.com") + "\n000 curl=7\n")})
            )
        )
    with pytest.raises(RuntimeError, match="still resolves elsewhere: \\(nothing\\)"):
        asyncio.run(sandbox.close_code_hosts(ScriptedBox({**hosts, gh: (0, "\n\n000 curl=6\n")})))
    # the index is proved too: a github closed beside a pypi that answers is aided (pip download)
    pypi_open = {"getent ahosts pypi.org": (0, ours.format(h="pypi.org") + "\n200 curl=0\n")}
    index = ScriptedBox({**hosts, **pypi_open, gh: (0, "127.0.0.1 github.com\n\n000 curl=7\n")})
    with pytest.raises(RuntimeError, match="pypi.org is reachable"):
        asyncio.run(sandbox.close_code_hosts(index))
    # reopening deletes exactly the marked lines and proves none is left
    reopened = ScriptedBox({"grep -v": (0, "")})
    asyncio.run(sandbox.open_code_hosts(reopened))
    assert reopened.ran == [  # rewritten through cat, never renamed over: /etc/hosts is a bind mount
        "grep -v -F '# lion-unaided' /etc/hosts > /tmp/lion-hosts; cat /tmp/lion-hosts > /etc/hosts"
        " && rm -f /tmp/lion-hosts && ! grep -q -F '# lion-unaided' /etc/hosts"
    ]
    assert "sed -i" not in reopened.ran[0]
    with pytest.raises(RuntimeError, match="reopen"):
        asyncio.run(sandbox.open_code_hosts(ScriptedBox({"grep -v": (1, "left")})))


def _fake_swebench(monkeypatch) -> list:
    """swebench's grading modules as stubs that grade every prediction resolved; returns the reports made."""
    reports: list = []

    def get_eval_report(test_spec, prediction, test_log_path, include_tests_status):
        reports.append(prediction["instance_id"])
        return {prediction["instance_id"]: {"resolved": True, "patch_successfully_applied": True}}

    grading = types.ModuleType("swebench.harness.grading")
    grading.get_eval_report = get_eval_report
    utils = types.ModuleType("swebench.harness.utils")
    utils.make_test_spec = lambda row: row
    for name, mod in {
        "swebench": types.ModuleType("swebench"),
        "swebench.harness": types.ModuleType("swebench.harness"),
        "swebench.harness.grading": grading,
        "swebench.harness.utils": utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return reports


class ResetBox:
    """A box whose clean-tree reset exits `reset_rc`; everything else succeeds."""

    workdir, prelude, dead = "/testbed", "", None

    def __init__(self, reset_rc: int):
        self.reset_rc, self.ran = reset_rc, []

    async def run(self, cmd, *, timeout=120.0, env=None):
        self.ran.append(cmd)
        if cmd.startswith("git -c core.fileMode=false checkout"):
            return self.reset_rc, "error: unable to unlink old 'tests/test_x.py': Permission denied\n"
        return 0, ""

    async def write(self, path, text):
        pass


def test_failed_reset_is_not_graded(monkeypatch, tmp_path):
    # the reset's rc was once discarded and the unreset tree graded GRADED
    reports = _fake_swebench(monkeypatch)
    row = {"instance_id": "x__x-1", "eval_script": "true"}
    box = ResetBox(reset_rc=1)
    verdict = asyncio.run(sandbox.grade(box, row, "diff", "m", tmp_path / "log.txt"))
    assert verdict["status"] == "RESET_FAIL" and verdict["resolved"] is False
    assert "reset exited 1" in verdict["error"] and "Permission denied" in verdict["error"]
    assert len(box.ran) == 1 and reports == []  # nothing applied, nothing run, nothing reported
    assert "Permission denied" in (tmp_path / "log.txt").read_text()
    # the control: a reset that succeeds is graded as before
    clean = ResetBox(reset_rc=0)
    assert asyncio.run(sandbox.grade(clean, row, "diff", "m", tmp_path / "log2.txt"))["status"] == "GRADED"
    assert reports == ["x__x-1"]


def test_a_ledger_row_counts_only_when_it_says_it_came_from_this_run(tmp_path):
    import json

    import run_set

    ledger = tmp_path / "ledger.jsonl"
    run = {"unaided": True, "model": "m", "backend": "openrouter", "harness": "lndl"}
    assert run_set.ledger_clash(ledger, run) == []
    rows = [
        {
            "instance_id": "old-1",
            "resolved": True,
            "model": "m",
            "backend": "openrouter",
        },  # no policy verdict
        {"instance_id": "aided-1", "resolved": True, "aided": True, "model": "m", "backend": "openrouter"},
        {
            "instance_id": "unaided-1",
            "resolved": False,
            "aided": False,
            "model": "m",
            "backend": "openrouter",
        },
        {"instance_id": "cli-1", "resolved": False, "aided": False, "model": "m", "backend": "codex-cli"},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert [c.split()[0] for c in run_set.ledger_clash(ledger, run)] == ["old-1", "aided-1", "cli-1"]
    aided = {**run, "unaided": False}
    assert [c.split()[0] for c in run_set.ledger_clash(ledger, aided)] == ["old-1", "unaided-1", "cli-1"]
    cli = {**run, "harness": "codex-cli"}
    assert [c.split()[0] for c in run_set.ledger_clash(ledger, cli)] == ["old-1", "aided-1", "unaided-1"]
