"""The naked arm: a harness failure after paid work still records the patch on disk and the spend."""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from tests._git import git, repo
from tests._swe import run_naked

ROW = {"instance_id": "x__x-1", "image": "img", "problem_statement": "It breaks."}
ENV = {
    "subtype": "error_max_turns",
    "num_turns": 3,
    "total_cost_usd": 0.25,
    "usage": {"input_tokens": 10, "output_tokens": 4},
}


def _fake_docker(monkeypatch):
    """`docker cp` puts a repository at the host path; every other docker call is a no-op."""
    calls = []

    async def docker(*argv, check=True):
        calls.append(argv)
        if argv[0] == "cp":
            repo(Path(argv[2]), {"a.py": "x = 1\n"})
        return 0, ""

    monkeypatch.setattr(run_naked, "_docker", docker)
    return calls


class Harness:
    """Edits a.py in its cwd, records a paid envelope, and then raises (or returns) as `fail` says."""

    def __init__(self, *, model, tools, max_turns, cwd, timeout):
        self.cwd, self.calls = Path(cwd), []

    commit = False

    async def __call__(self, prompt: str) -> str:
        (self.cwd / "a.py").write_text("x = 2\n")
        if Harness.commit:  # the model committed its fix: HEAD moved past the commit it was handed
            git(self.cwd, "commit", "-q", "-am", "fix")
        self.calls.append(dict(ENV))
        if Harness.fail:
            raise TimeoutError("the harness hung after its last turn")
        return "done"


def test_a_harness_failure_after_paid_work_keeps_the_patch_and_the_spend(tmp_path, monkeypatch):
    _fake_docker(monkeypatch)
    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Harness)
    Harness.fail = True
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert rec["error"] == "TimeoutError: the harness hung after its last turn"
    assert rec["cost_usd"] == 0.25 and rec["turns"] == 3 and rec["tokens"]["output"] == 4
    assert "+x = 2" in rec["model_patch"]
    assert json.loads((tmp_path / "x__x-1.json").read_text())["model_patch"] == rec["model_patch"]
    # the control: the same work, returned, records the same evidence and no error
    Harness.fail = False
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert "error" not in rec and rec["cost_usd"] == 0.25 and "+x = 2" in rec["model_patch"]
    assert rec["outcome"] == "error_max_turns" and rec["final_text"] == "done"


def test_a_commit_the_model_made_stays_in_the_naked_prediction(tmp_path, monkeypatch):
    _fake_docker(monkeypatch)
    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Harness)
    Harness.fail, Harness.commit = False, True
    try:
        rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    finally:
        Harness.commit = False
    assert "+x = 2" in rec["model_patch"] and len(rec["base"]) == 40


def test_the_naked_patch_keeps_odd_names_and_drops_staged_bytecode(tmp_path, monkeypatch):
    _fake_docker(monkeypatch)

    class Odd(Harness):
        async def __call__(self, prompt):
            (self.cwd / "new file.py").write_text("y = 1\n")  # a name with a space
            (self.cwd / "-n.py").write_text("z = 1\n")  # a name that reads as an option
            (self.cwd / "__pycache__").mkdir()
            (self.cwd / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00pyc")
            git(self.cwd, "add", "__pycache__")  # staged bytecode
            self.calls.append(dict(ENV))
            return "done"

    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Odd)
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert "diff_error" not in rec and "error" not in rec, rec
    patch = rec["model_patch"]
    assert "b/new file.py" in patch and "+y = 1" in patch
    assert "b/-n.py" in patch and "+z = 1" in patch
    assert ".pyc" not in patch and "__pycache__" not in patch


def test_an_unknown_instance_is_refused_before_any_run(tmp_path, monkeypatch):
    data = tmp_path / "data.json"
    data.write_text(json.dumps([ROW]))
    monkeypatch.setattr(run_naked, "DATA", data)
    monkeypatch.setattr(run_naked, "RESULTS", tmp_path / "results")
    solved = []

    async def solve(row, *a):
        solved.append(row["instance_id"])
        return {"instance_id": row["instance_id"], "model_patch": "", "seconds": 0.0}

    monkeypatch.setattr(run_naked, "solve", solve)
    args = argparse.Namespace(instances=[], run_id="r", parallel=2, model="m", max_turns=3, oracle=False)
    with pytest.raises(SystemExit, match="nope"):
        asyncio.run(run_naked.main(argparse.Namespace(**{**vars(args), "instances": ["x__x-1", "nope"]})))
    assert solved == []  # refused before any instance started paid work
    asyncio.run(run_naked.main(argparse.Namespace(**{**vars(args), "instances": ["x__x-1"]})))
    assert solved == ["x__x-1"]  # the control: a known instance runs


def test_a_failure_before_any_turn_records_the_error_alone(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)

    class Early(Harness):
        async def __call__(self, prompt):
            raise RuntimeError("no key")

    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Early)
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert rec["error"] == "RuntimeError: no key" and "cost_usd" not in rec and rec["model_patch"] == ""
    assert calls[-1][:3] == ("container", "remove", "--force")  # the box is still cleaned up


def test_a_cancellation_after_paid_work_keeps_the_patch_and_the_spend_and_still_propagates(
    tmp_path, monkeypatch
):
    calls = _fake_docker(monkeypatch)

    class Cancelled(Harness):
        async def __call__(self, prompt):
            (self.cwd / "a.py").write_text("x = 2\n")
            self.calls.append(dict(ENV))
            raise asyncio.CancelledError

    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    rec = json.loads((tmp_path / "x__x-1.json").read_text())
    assert rec["error"] == "CancelledError: " and rec["cost_usd"] == 0.25 and "+x = 2" in rec["model_patch"]
    assert calls[-1][:3] == ("container", "remove", "--force")


def test_the_source_container_is_removed_when_the_baseline_read_refuses(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)

    async def refused(box):
        raise RuntimeError("no baseline")

    monkeypatch.setattr(run_naked, "baseline", refused)
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert (
        rec["error"] == "RuntimeError: no baseline" and rec["model_patch"] == "" and "diff_error" not in rec
    )
    removed = [c[3] for c in calls if c[:3] == ("container", "remove", "--force")]
    assert removed == [calls[0][4], calls[0][4][: -len("-src")]]  # the copy's container, then the run's
    assert "run" not in [c[0] for c in calls]  # nothing was started for a model


def test_a_trace_git_writes_beside_the_baseline_read_is_no_part_of_the_commit(tmp_path, monkeypatch):
    # git writes a trace or a warning on stderr beside a read that succeeded, into the same output
    head = repo(tmp_path / "r", {"a.py": "x = 1\n"})
    monkeypatch.setenv("GIT_TRACE", "1")
    assert asyncio.run(run_naked.baseline(run_naked.HostBox(tmp_path / "r"))) == head


def test_a_tree_whose_patch_could_not_be_read_is_kept_and_named(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)
    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Harness)
    Harness.fail = False

    async def unreadable(box, base):
        raise RuntimeError("git diff exited 128: fatal: bad object")

    monkeypatch.setattr(run_naked, "git_diff", unreadable)
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert rec["error"] == "RuntimeError: git diff exited 128: fatal: bad object"
    assert rec["diff_error"] == rec["error"] and rec["model_patch"] == "" and rec["cost_usd"] == 0.25
    host = Path(rec["host"])
    assert (host / "a.py").read_text() == "x = 2\n"  # the model's edit, where the record says it is
    assert calls[-1][:3] == ("container", "remove", "--force")
    assert json.loads((tmp_path / "x__x-1.json").read_text())["host"] == rec["host"]
    shutil.rmtree(host)


def test_a_cancellation_during_cleanup_still_leaves_the_record(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)
    docker = run_naked._docker

    async def cancelled_at_remove(*argv, check=True):
        if argv[:3] == ("container", "remove", "--force") and not argv[3].endswith("-src"):
            calls.append(argv)
            raise asyncio.CancelledError  # the task was cancelled while this await was pending
        return await docker(*argv, check=check)

    monkeypatch.setattr(run_naked, "_docker", cancelled_at_remove)
    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Harness)
    Harness.fail = False
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    rec = json.loads((tmp_path / "x__x-1.json").read_text())
    assert "error" not in rec and rec["cost_usd"] == 0.25 and "+x = 2" in rec["model_patch"]
    host = Path(next(c[2] for c in calls if c[0] == "cp"))
    assert not host.exists()  # the host copy still went, its patch being on the record
