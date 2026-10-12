"""The bench over a Runta box: `testbed` builds one with the testbed's shape, the instance's image is
found or built, and `run_set --where runta` reaches the testbed through `one`."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hub import RuntaSandbox
from hub.tools import Egress
from tests._swe import DIFF, Started
from tests._swe import run_set as swe_run_set
from tests._swe import sandbox as swe_sandbox
from tests.test_runta import Resp, install_httpx, stub_runta
from tests.test_swe_run_set import _args, _main, _one

pytestmark = pytest.mark.usefixtures("bench_store")

# --- lion1/bench/swe/sandbox.py: testbed over Runta ---------------------------------------------


def test_testbed_runta_gives_a_runta_box_with_the_testbed_shape():
    box = swe_sandbox.testbed("img-1", "runta", cpu=4, memory=6, label="swe-x")
    assert isinstance(box, RuntaSandbox)
    assert (box.workdir, box.prelude) == (swe_sandbox.WORKDIR, swe_sandbox.PRELUDE)
    assert (box.cpu, box.memory, box.name, box.image) == (4, 6, "swe-x", "img-1")


def test_testbed_runta_carries_the_code_hosts_as_its_policy_only_when_unaided():
    unaided = swe_sandbox.testbed("img-1", "runta", unaided=True)
    assert unaided.egress == Egress(mode="denylist", hosts=swe_sandbox.CODE_HOSTS)
    assert swe_sandbox.testbed("img-1", "runta").egress is None


def test_testbed_refuses_an_unknown_where():
    with pytest.raises(ValueError, match="nowhere"):
        swe_sandbox.testbed("img", "nowhere")


def test_runta_name_is_swe_prefixed_lowercase_after_the_owner():
    assert swe_sandbox.runta_name({"instance_id": "django__django-11951"}) == "swe-django-11951"


def test_runta_image_returns_a_found_image_without_building(monkeypatch):
    seen = []

    async def fake_find_image(name, *, api=None):
        seen.append(name)
        return "img-existing"

    async def fail_build_image(*a, **k):
        raise AssertionError("build_image was called although an image was found")

    monkeypatch.setattr(swe_sandbox, "find_image", fake_find_image)
    monkeypatch.setattr(swe_sandbox, "build_image", fail_build_image)
    row = {"instance_id": "org__has-image-1", "image": "base:1"}
    assert asyncio.run(swe_sandbox.runta_image(row)) == "img-existing"
    assert seen == ["swe-has-image-1"]


def test_runta_image_builds_from_the_rows_image_when_none_is_found(monkeypatch):
    async def fake_find_image(name, *, api=None):
        return None

    built = []

    async def fake_build_image(name, dockerfile, *, architecture="x86_64", timeout=3600.0, api=None):
        built.append((name, dockerfile, architecture))
        return "img-built"

    monkeypatch.setattr(swe_sandbox, "find_image", fake_find_image)
    monkeypatch.setattr(swe_sandbox, "build_image", fake_build_image)
    row = {"instance_id": "org__needs-build-1", "image": "base:2"}
    assert asyncio.run(swe_sandbox.runta_image(row)) == "img-built"
    assert built == [("swe-needs-build-1", "FROM base:2\n", "x86_64")]


# --- lion1/bench/swe/run_set.py: --where runta --------------------------------------------------


def test_the_cli_parser_accepts_where_runta():
    script = Path(__file__).resolve().parents[1] / "bench" / "swe" / "run_set.py"
    out = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "{local,daytona,runta}" in out.stdout


def test_where_runta_reaches_testbed_through_one(tmp_path, monkeypatch):
    seen = []

    def fake_testbed(image, where, *, cpu, memory, label, unaided):
        seen.append((image, where, cpu, memory, label, unaided))
        return Started()

    async def fake_runta_image(row, *, timeout=3600.0):
        return "runta-" + row["image"]  # a distinct id: the box is made from what this returned

    async def fake_solve(row, *a, **k):
        return {"instance_id": row["instance_id"], "model_patch": ""}

    monkeypatch.setattr(swe_run_set, "testbed", fake_testbed)
    monkeypatch.setattr(swe_run_set, "runta_image", fake_runta_image)
    monkeypatch.setattr(swe_run_set, "solve", fake_solve)
    _one(_args(where="runta"), tmp_path)
    assert seen == [("runta-img", "runta", 2, 2, "lionagi-swe-r1", False)]
    seen.clear()
    _one(_args(where="runta", unaided=True), tmp_path)
    assert [unaided for *_, unaided in seen] == [True]


# --- the unaided run's closure at the service ------------------------------------------------

CODE_HOST_POLICY = Egress(mode="denylist", hosts=swe_sandbox.CODE_HOSTS)


def _runta_one(monkeypatch, tmp_path, *, unaided, reported=None, runtimes=("rt-1",)):
    """`one` over the real `testbed` and a Runta runtime made by the stubbed SDK and service, which
    reports `reported` as every runtime's policy: the create requests the service was sent, the SDK
    stub, and the instances the model was given."""
    monkeypatch.setenv("RUNTA_TOKEN", "tok")
    monkeypatch.setenv("RUNTA_ENDPOINT", "http://api.test")
    mod = stub_runta(tmp_path)
    monkeypatch.setitem(sys.modules, "runta", mod)
    for rt_id in runtimes:
        mod.preset_policy[rt_id] = reported
    calls, _, _ = install_httpx(monkeypatch, [Resp(200, {"data": {"id": i}}) for i in runtimes])
    monkeypatch.setattr(swe_sandbox, "WORKDIR", str(tmp_path / "testbed"))
    solved = []

    async def fake_runta_image(row, *, timeout=3600.0):
        return "img-runta"

    async def fake_solve(row, *a, **k):
        solved.append(row["instance_id"])
        return {"instance_id": row["instance_id"], "model_patch": ""}

    async def hosts(box):
        return "closed"  # the /etc/hosts edit runs in the guest; here the runtime is this machine

    monkeypatch.setattr(swe_run_set, "runta_image", fake_runta_image)
    monkeypatch.setattr(swe_run_set, "solve", fake_solve)
    for name in ("close_code_hosts", "open_code_hosts"):
        monkeypatch.setattr(swe_run_set, name, hosts)
    rec = _one(_args(where="runta", unaided=unaided), tmp_path)
    return rec, mod, calls, solved


@pytest.mark.parametrize(
    ("unaided", "sent"),
    [(True, CODE_HOST_POLICY.api()), (False, None)],
    ids=["unaided sends the denylist", "aided sends none"],
)
def test_a_runta_run_sends_the_code_host_denylist_with_the_create_only_when_unaided(
    tmp_path, monkeypatch, unaided, sent
):
    # the service reports back the policy it was sent, or none was sent
    rec, mod, calls, solved = _runta_one(monkeypatch, tmp_path, unaided=unaided, reported=sent)
    assert [c["method"] for c in calls] == ["POST"]
    assert calls[0]["json"].get("egress_policy") == sent
    assert "error" not in rec and solved == ["a"]
    assert mod.runtimes_store["rt-1"].set_policy_calls == 0 and mod.runtimes_store["rt-1"].deleted is True


def _policy_box(*, closed_probes: int, egress: Egress | None = CODE_HOST_POLICY):
    """A Runta box whose commands are scripted and whose policy changes are recorded: the first
    `closed_probes` fetches fail as under the denylist, the ones after answer."""
    box = RuntaSandbox(workdir="/testbed", egress=egress)
    ran, set_to = [], []

    async def run(cmd, *, timeout=120, env=None):
        ran.append(cmd)
        if not cmd.startswith("getent"):
            return 0, ""  # the /etc/hosts rewrite
        shut = sum(c.startswith("getent") for c in ran) <= closed_probes
        return 0, "140.82.112.3 STREAM x\n\n" + ("000 curl=7\n" if shut else "200 curl=0\n")

    async def set_egress(policy):
        set_to.append(policy)
        box.egress = policy
        return policy

    box.run, box.set_egress = run, set_egress
    return box, ran, set_to


def test_reopening_a_runta_box_empties_its_policy_and_waits_until_the_hosts_answer(
    monkeypatch,
):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    box, ran, set_to = _policy_box(closed_probes=2)  # the first pass over both probes still fails
    asyncio.run(swe_sandbox.open_code_hosts(box))
    assert set_to == [Egress(mode="denylist")] and box.egress == Egress(mode="denylist")
    assert [c.split()[2] for c in ran if c.startswith("getent")] == [
        "github.com;",
        "pypi.org;",
        "github.com;",
        "pypi.org;",
    ]
    assert slept == [3]  # one wait, between the failed pass and the one that answered


def test_a_runta_policy_that_does_not_lift_in_time_refuses_the_grade(monkeypatch):
    box, ran, set_to = _policy_box(closed_probes=100)
    with pytest.raises(RuntimeError, match="github.com, pypi.org still did not answer in 0s"):
        asyncio.run(swe_sandbox.open_code_hosts(box, within=0))
    assert len(set_to) == 1
    assert not [c for c in ran if c.startswith("getent")]  # nothing was left, so no probe was started


def _slow_probe_box(*, takes: float, shut: bool):
    """A Runta box whose probes each take `takes` seconds unless the timeout they are handed ends them first
    (rc 124, no output), as a box's own does; the timeouts handed in are recorded."""
    box = RuntaSandbox(workdir="/testbed", egress=CODE_HOST_POLICY)
    timeouts = []

    async def run(cmd, *, timeout=120, env=None):
        if not cmd.startswith("getent"):
            return 0, ""
        timeouts.append(timeout)
        try:
            await asyncio.wait_for(asyncio.sleep(takes), timeout)
        except TimeoutError:
            return 124, ""
        return 0, "140.82.112.3 STREAM x\n\n" + ("000 curl=7\n" if shut else "200 curl=0\n")

    async def set_egress(policy):
        box.egress = policy
        return policy

    box.run, box.set_egress = run, set_egress
    return box, timeouts


def test_a_probe_that_outlasts_the_wait_refuses_the_grade_within_it():
    box, timeouts = _slow_probe_box(takes=1.5, shut=False)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="github.com, pypi.org still did not answer in 0.2s"):
        asyncio.run(swe_sandbox.open_code_hosts(box, within=0.2))
    assert time.monotonic() - t0 < 1.0  # the first probe was ended at what was left of 0.2 s
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 0.2  # and with nothing left the second was not started


def test_the_wait_between_passes_never_runs_past_the_bound():
    box, timeouts = _slow_probe_box(takes=0.0, shut=True)  # every probe answers at once, and is shut
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="github.com, pypi.org still did not answer in 0.2s"):
        asyncio.run(swe_sandbox.open_code_hosts(box, within=0.2))
    assert time.monotonic() - t0 < 1.0  # the 3 s wait between passes is cut to what is left
    assert len(timeouts) == 2 and all(0 < x <= 0.2 for x in timeouts)  # one pass, each probe within the bound


def _graded_run(monkeypatch, tmp_path, *, reopens: bool):
    """`one` for an unaided Runta run whose model left a patch, with the hosts' reopen succeeding or
    raising what `open_code_hosts` raises when they never answer, and a `grade` that records its calls."""
    graded = []

    async def fake_runta_image(row, *, timeout=3600.0):
        return "img-runta"

    async def fake_solve(row, *a, **k):
        return {"instance_id": row["instance_id"], "model_patch": DIFF}

    async def hosts(box):
        return "closed"

    async def reopen(box):
        if not reopens:
            raise RuntimeError("github.com, pypi.org still did not answer in 90s")

    async def fake_grade(box, row, patch, model_name, log_path, **kw):
        graded.append(patch)
        return {"resolved": True, "status": "RESOLVED_FULL"}

    monkeypatch.setattr(swe_run_set, "testbed", lambda *a, **k: Started())
    monkeypatch.setattr(swe_run_set, "runta_image", fake_runta_image)
    monkeypatch.setattr(swe_run_set, "solve", fake_solve)
    monkeypatch.setattr(swe_run_set, "close_code_hosts", hosts)
    monkeypatch.setattr(swe_run_set, "open_code_hosts", reopen)
    monkeypatch.setattr(swe_run_set, "grade", fake_grade)
    return _one(_args(where="runta", unaided=True), tmp_path), graded


def test_the_runner_does_not_grade_a_run_whose_hosts_did_not_reopen_and_names_the_refusal(
    tmp_path, monkeypatch
):
    rec, graded = _graded_run(monkeypatch, tmp_path, reopens=False)
    assert graded == [] and "grade" not in rec and rec["resolved"] is False
    assert rec["error"] == "RuntimeError: github.com, pypi.org still did not answer in 90s"


def test_the_runner_grades_a_run_whose_hosts_reopened_once(tmp_path, monkeypatch):
    rec, graded = _graded_run(monkeypatch, tmp_path, reopens=True)  # the control of the test above
    assert graded == [DIFF] and rec["resolved"] is True and "error" not in rec


def test_reopening_a_runta_box_without_a_policy_only_rewrites_the_hosts_file():
    box, ran, set_to = _policy_box(closed_probes=0, egress=None)
    asyncio.run(swe_sandbox.open_code_hosts(box))
    assert set_to == [] and len(ran) == 1 and ran[0].startswith("grep -v")


def test_an_unaided_runta_runtime_with_another_policy_is_refused_before_the_model_runs(tmp_path, monkeypatch):
    rec, mod, calls, solved = _runta_one(
        monkeypatch, tmp_path, unaided=True, reported=None, runtimes=("rt-1", "rt-2")
    )
    assert solved == []  # neither attempt reached the model
    assert rec["error"].startswith("SandboxDead") and "denylist" in rec["error"] and "none" in rec["error"]
    assert [r["error"][:11] for r in rec["retried"]] == ["SandboxDead"]
    assert [mod.runtimes_store[i].deleted for i in ("rt-1", "rt-2")] == [True, True]
    assert all(c["json"]["egress_policy"] == CODE_HOST_POLICY.api() for c in calls)


def test_the_codex_arm_on_an_unaided_runta_launch_is_refused_before_any_box(tmp_path, monkeypatch):
    # its binary is downloaded from github.com, which the unaided box's policy denies from the start
    async def fake_runta_image(row, *, timeout=3600.0):
        return row["image"]

    ran = _main(monkeypatch, tmp_path, [{"instance_id": "a", "repo": "x", "image": "img"}])
    monkeypatch.setattr(swe_run_set, "runta_image", fake_runta_image)
    with pytest.raises(SystemExit, match="--aided"):
        asyncio.run(swe_run_set.main(_args(where="runta", harness="codex-cli", unaided=True)))
    assert ran == [] and not (tmp_path / "results").exists()
    # the controls: the same arm aided, and the loop arm unaided, both launch on Runta
    asyncio.run(swe_run_set.main(_args(where="runta", harness="codex-cli", unaided=False, run_id="r2")))
    asyncio.run(swe_run_set.main(_args(where="runta", harness="lndl", unaided=True, run_id="r3")))
    assert [(a.harness, a.unaided) for _, a in ran] == [("codex-cli", False), ("lndl", True)]
