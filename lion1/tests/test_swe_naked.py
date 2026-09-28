"""The naked arm: a harness failure after paid work still records the patch on disk and the spend."""

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "swe"))

import run_naked  # noqa: E402

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
            host = Path(argv[2])
            env = {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
                "PATH": __import__("os").environ["PATH"],
            }
            subprocess.run(["git", "init", "-q"], cwd=host, env=env, check=True)
            (host / "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "a.py"], cwd=host, env=env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=host, env=env, check=True)
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
            env = {
                "GIT_AUTHOR_NAME": "m",
                "GIT_AUTHOR_EMAIL": "m@example.com",
                "GIT_COMMITTER_NAME": "m",
                "GIT_COMMITTER_EMAIL": "m@example.com",
                "PATH": __import__("os").environ["PATH"],
            }
            subprocess.run(["git", "commit", "-q", "-am", "fix"], cwd=self.cwd, env=env, check=True)
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


def test_a_git_read_that_fails_is_an_error_not_an_empty_patch(tmp_path, monkeypatch):
    base = "b" * 40
    seen = []

    def run(argv, *, cwd, capture_output, text):
        seen.append(argv[1:])
        rc, out, err = 0, "", ""
        if argv[1] == "diff" and "--no-index" in argv:
            rc, out = 1, "diff --git a/new.py b/new.py\n+x = 2\n"  # --no-index: 1 is a difference
            if run.new_fails:
                rc, out, err = 128, "", "fatal: bad object"
        elif argv[1] == "diff":
            out = "diff --git a/a.py b/a.py\n-x = 1\n+x = 2\n"
            if run.tracked_fails:
                rc, out, err = 128, out[:20], "fatal: bad revision"  # partial output, then the error
        elif argv[1] == "ls-files":
            out = "new.py\n"
        return subprocess.CompletedProcess(argv, rc, out, err)

    monkeypatch.setattr(run_naked.subprocess, "run", run)
    run.tracked_fails = run.new_fails = False
    patch = run_naked._diff(tmp_path, base)  # the control: both reads healthy
    assert "+x = 2" in patch and "new.py" in patch and seen[0] == ["diff", "--binary", base]
    run.tracked_fails = True
    with pytest.raises(RuntimeError, match="git diff exited 128: fatal: bad revision"):
        run_naked._diff(tmp_path, base)
    run.tracked_fails, run.new_fails = False, True
    with pytest.raises(RuntimeError, match="git diff exited 128: fatal: bad object"):
        run_naked._diff(tmp_path, base)


def test_the_source_container_is_removed_when_the_baseline_read_refuses(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)
    monkeypatch.setattr(
        run_naked, "_baseline", lambda host: (_ for _ in ()).throw(RuntimeError("no baseline"))
    )
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert (
        rec["error"] == "RuntimeError: no baseline" and rec["model_patch"] == "" and "diff_error" not in rec
    )
    removed = [c[3] for c in calls if c[:3] == ("container", "remove", "--force")]
    assert removed == [calls[0][4], calls[0][4][: -len("-src")]]  # the copy's container, then the run's
    assert "run" not in [c[0] for c in calls]  # nothing was started for a model


def test_a_tree_whose_patch_could_not_be_read_is_kept_and_named(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)
    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Harness)
    Harness.fail = False
    real = run_naked.subprocess.run

    def run(argv, **kw):
        if argv[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: bad object")
        return real(argv, **kw)

    monkeypatch.setattr(run_naked.subprocess, "run", run)
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


def _harness(monkeypatch, resolves, report=None) -> list[str]:
    """The harness as it grades: an instance whose per-instance report exists under the run id is
    skipped, and the run's report is written fresh from the per-instance ones. `report` replaces
    the run's report when given."""
    ids: list[str] = []

    def run(argv, *, capture_output, text, cwd, timeout):
        rid = argv[argv.index("--run_id") + 1]
        ids.append(rid)
        preds = [
            json.loads(line)
            for line in Path(argv[argv.index("--predictions_path") + 1]).read_text().splitlines()
        ]
        resolved = []
        for p in preds:
            done = Path(cwd) / "logs" / "run_evaluation" / rid / "m" / p["instance_id"] / "report.json"
            if not done.exists():
                done.parent.mkdir(parents=True)
                done.write_text(json.dumps({p["instance_id"]: {"resolved": resolves(p["model_patch"])}}))
            if json.loads(done.read_text())[p["instance_id"]]["resolved"]:
                resolved.append(p["instance_id"])
        doc = {"submitted_ids": sorted(p["instance_id"] for p in preds), "resolved_ids": resolved}
        (Path(cwd) / f"m.{rid}.json").write_text(json.dumps(report if report is not None else doc))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_naked.subprocess, "run", run)
    return ids


def test_new_predictions_under_a_reused_run_id_are_graded_not_read_from_an_earlier_evaluation(
    tmp_path, monkeypatch
):
    preds = tmp_path / "predictions.jsonl"

    def predict(patch: str) -> None:
        preds.write_text(
            json.dumps({"instance_id": "x__x-1", "model_name_or_path": "m", "model_patch": patch}) + "\n"
        )

    ids = _harness(monkeypatch, lambda patch: "+x = 2" in patch)
    earlier = tmp_path / "logs" / "run_evaluation" / "r1" / "m" / "x__x-1" / "report.json"
    earlier.parent.mkdir(parents=True)
    earlier.write_text(json.dumps({"x__x-1": {"resolved": True}}))  # an earlier evaluation under this run id
    predict("+x = 3\n")
    assert run_naked.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == []
    predict("+x = 2\n")  # new predictions, the same run id
    assert run_naked.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == ["x__x-1"]
    predict("+x = 3\n")  # the first predictions again: their own earlier grade is theirs
    assert run_naked.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == []
    assert ids[0] == ids[2] != ids[1] and all(i.startswith("r1.") for i in ids)
    predict("+x = 2\n")  # a run id holding glob syntax names its report literally
    assert run_naked.oracle(preds, "r[1]", ["x__x-1"], 1)["resolved_ids"] == ["x__x-1"]


def test_a_report_that_does_not_name_the_predictions_handed_in_is_refused(tmp_path, monkeypatch):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(
        json.dumps({"instance_id": "x__x-1", "model_name_or_path": "m", "model_patch": "+x = 2\n"}) + "\n"
    )
    for report in ({"submitted_ids": ["y__y-2"], "resolved_ids": ["y__y-2"]}, {"resolved_ids": ["x__x-1"]}):
        _harness(monkeypatch, lambda patch: True, report)
        with pytest.raises(RuntimeError, match=r"not the predictions handed in \['x__x-1'\]"):
            run_naked.oracle(preds, "r1", ["x__x-1"], 1)
    _harness(monkeypatch, lambda patch: True)  # the control: the harness's own report on these predictions
    assert run_naked.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == ["x__x-1"]


def test_the_oracle_reads_only_a_report_its_own_evaluation_wrote(tmp_path, monkeypatch):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"instance_id": "x__x-1", "model_patch": "+x = 2\n"}) + "\n")
    digest = hashlib.sha256(preds.read_bytes()).hexdigest()[:16]
    report = tmp_path / f"claude-code-naked-m.r1.{digest}.json"
    report.write_text(json.dumps({"resolved_ids": ["x__x-1"]}))  # an earlier evaluation under this run id

    def evaluator(rc: int, writes: dict | None) -> None:
        def run(argv, *, capture_output, text, cwd, timeout):
            if writes is not None:
                report.write_text(json.dumps(writes))
            return subprocess.CompletedProcess(argv, rc, "evaluated\n", "failed\n" if rc else "")

        monkeypatch.setattr(run_naked.subprocess, "run", run)

    evaluator(1, None)  # the evaluation fails and writes nothing
    with pytest.raises(RuntimeError, match="the harness exited 1; see oracle_r1.log"):
        run_naked.oracle(preds, "r1", ["x__x-1"], 1)
    assert (tmp_path / "oracle_r1.log").read_text() == "evaluated\n\n--- stderr ---\nfailed\n"
    evaluator(1, {"resolved_ids": []})  # a report beside a failed exit is not a grade either
    with pytest.raises(RuntimeError, match="the harness exited 1"):
        run_naked.oracle(preds, "r1", ["x__x-1"], 1)
    report.write_text(json.dumps({"resolved_ids": ["x__x-1"]}))
    evaluator(0, None)  # a clean exit that wrote no report: the one on disk is still the earlier one
    with pytest.raises(RuntimeError, match="no harness report from this evaluation"):
        run_naked.oracle(preds, "r1", ["x__x-1"], 1)
    own = {"submitted_ids": ["x__x-1"], "resolved_ids": []}
    evaluator(0, own)  # the control: this evaluation's own report, under the same name
    assert run_naked.oracle(preds, "r1", ["x__x-1"], 1) == own


@pytest.mark.parametrize("streams", [(b"so far\n", b"slow\n"), ("so far\n", "slow\n"), (None, None)])
def test_an_evaluation_that_times_out_leaves_its_log_and_the_timeout_stands(tmp_path, monkeypatch, streams):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"instance_id": "x__x-1", "model_patch": "+x = 2\n"}) + "\n")

    def run(argv, *, capture_output, text, cwd, timeout):
        raise subprocess.TimeoutExpired(argv, timeout, output=streams[0], stderr=streams[1])

    monkeypatch.setattr(run_naked.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        run_naked.oracle(preds, "r1", ["x__x-1"], 1)
    so_far, slow = ("so far\n", "slow\n") if streams[0] else ("", "")
    assert (tmp_path / "oracle_r1.log").read_text() == f"{so_far}\n--- stderr ---\n{slow}"
