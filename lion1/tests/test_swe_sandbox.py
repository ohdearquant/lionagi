"""The bench's box is the hub's: `testbed` picks docker or Daytona over the eval image, with the
testbed environment as the prelude and /testbed as the directory; a box that never comes up is
replaced and a box that dies is named (through the hub's own start logic)."""

import asyncio
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hub.tools import DaytonaSandbox, DockerSandbox
from tests import _git
from tests._swe import sandbox

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
    # a box with no curl prints the rc alone: no fetch was tried, so nothing proves the host closed
    for fetch in ("\n curl=127\n", "\n curl=126\n", "\n\n"):
        blind = ScriptedBox({**hosts, **pypi_closed, gh: (0, ours.format(h="github.com") + fetch)})
        with pytest.raises(RuntimeError, match="github.com was not fetched"):
            asyncio.run(sandbox.close_code_hosts(blind))
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
        if cmd.startswith("git -c core.fileMode=false reset"):
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


class LocalGitBox:
    """A box over a real repository on this host: `exec` runs the argv here, `run` the shell line from
    the repository. The box's absolute scratch paths land under `scratch`."""

    prelude, dead = "", None

    def __init__(self, repo: Path, scratch: Path):
        self.workdir, self.scratch = str(repo), scratch

    def _local(self, cmd: str) -> str:
        return cmd.replace("/tmp/model.patch", str(self.scratch / "model.patch")).replace(
            "/eval.sh", str(self.scratch / "eval.sh")
        )

    async def exec(self, argv, *, stdin=None, timeout=120.0, env=None):
        r = subprocess.run(argv, input=stdin, capture_output=True, cwd=self.workdir)
        return {"rc": r.returncode, "stdout": r.stdout.decode(), "stderr": r.stderr.decode()}

    async def run(self, cmd, *, timeout=120.0, env=None):
        r = subprocess.run(
            ["bash", "-o", "pipefail", "-c", self._local(cmd)], capture_output=True, cwd=self.workdir
        )
        return r.returncode, r.stdout.decode() + r.stderr.decode()

    async def write(self, path, text):
        Path(self._local(path)).write_text(text)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository at its base commit, holding a.py; returns it and the base sha."""
    repo = tmp_path / "repo"
    return repo, _git.repo(repo, {"a.py": "x = 1\n"})


PATCH = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


@pytest.mark.parametrize("how", ["staged", "committed"])
def test_the_grade_resets_index_and_tree_to_the_baseline_so_the_models_own_git_state_is_gone(
    monkeypatch, tmp_path, how
):
    # a staged edit survived `git checkout -- .` (it restores from the index), a committed one moved
    # HEAD; either way the patch, holding the same edit, then failed to apply forward and `patch
    # --batch` applied it in reverse, and the tree the eval ran on was the base, graded as the fix
    reports = _fake_swebench(monkeypatch)
    repo, base = _repo(tmp_path)
    (repo / "a.py").write_text("x = 2\n")
    _git.git(repo, "add", "a.py")
    if how == "committed":
        _git.git(repo, "commit", "-q", "-m", "model")
    box = LocalGitBox(repo, tmp_path)
    row = {"instance_id": "x__x-1", "eval_script": "true", "base_commit": base}
    verdict = asyncio.run(sandbox.grade(box, row, PATCH, "m", tmp_path / "log.txt", base=base))
    assert verdict["status"] == "GRADED" and reports == ["x__x-1"]
    assert (repo / "a.py").read_text() == "x = 2\n"  # the patch, applied forward onto the base
    assert _git.git(repo, "rev-parse", "HEAD").strip() == base
    assert _git.git(repo, "diff", "--cached", "--stat") == ""


def test_the_grade_resets_to_the_rows_base_commit_when_the_caller_read_no_baseline(monkeypatch, tmp_path):
    _fake_swebench(monkeypatch)
    repo, base = _repo(tmp_path)
    (repo / "a.py").write_text("x = 2\n")
    _git.git(repo, "add", "a.py")
    row = {"instance_id": "x__x-1", "eval_script": "true", "base_commit": base}
    verdict = asyncio.run(sandbox.grade(LocalGitBox(repo, tmp_path), row, PATCH, "m", tmp_path / "log.txt"))
    assert verdict["status"] == "GRADED" and (repo / "a.py").read_text() == "x = 2\n"
    # and a row naming none resets to HEAD, which still clears the index
    (repo / "a.py").write_text("x = 3\n")
    _git.git(repo, "add", "a.py")
    verdict = asyncio.run(
        sandbox.grade(
            LocalGitBox(repo, tmp_path),
            {"instance_id": "x__x-1", "eval_script": "true"},
            PATCH,
            "m",
            tmp_path / "log2.txt",
        )
    )
    assert verdict["status"] == "GRADED" and (repo / "a.py").read_text() == "x = 2\n"
