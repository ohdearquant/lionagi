"""The oracle: the harness's own evaluation of a predictions file, read back only from a report of the
bytes it was handed and written since it began."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._swe import run_swe


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

    monkeypatch.setattr(run_swe.subprocess, "run", run)
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
    assert run_swe.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == []
    predict("+x = 2\n")  # new predictions, the same run id
    assert run_swe.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == ["x__x-1"]
    predict("+x = 3\n")  # the first predictions again: their own earlier grade is theirs
    assert run_swe.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == []
    assert ids[0] == ids[2] != ids[1] and all(i.startswith("r1.") for i in ids)
    predict("+x = 2\n")  # a run id holding glob syntax names its report literally
    assert run_swe.oracle(preds, "r[1]", ["x__x-1"], 1)["resolved_ids"] == ["x__x-1"]


def test_a_report_that_does_not_name_the_predictions_handed_in_is_refused(tmp_path, monkeypatch):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(
        json.dumps({"instance_id": "x__x-1", "model_name_or_path": "m", "model_patch": "+x = 2\n"}) + "\n"
    )
    for report in ({"submitted_ids": ["y__y-2"], "resolved_ids": ["y__y-2"]}, {"resolved_ids": ["x__x-1"]}):
        _harness(monkeypatch, lambda patch: True, report)
        with pytest.raises(RuntimeError, match=r"not the predictions handed in \['x__x-1'\]"):
            run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    _harness(monkeypatch, lambda patch: True)  # the control: the harness's own report on these predictions
    assert run_swe.oracle(preds, "r1", ["x__x-1"], 1)["resolved_ids"] == ["x__x-1"]


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

        monkeypatch.setattr(run_swe.subprocess, "run", run)

    evaluator(1, None)  # the evaluation fails and writes nothing
    with pytest.raises(RuntimeError, match="the harness exited 1; see oracle_r1.log"):
        run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    assert (tmp_path / "oracle_r1.log").read_text() == "evaluated\n\n--- stderr ---\nfailed\n"
    evaluator(1, {"resolved_ids": []})  # a report beside a failed exit is not a grade either
    with pytest.raises(RuntimeError, match="the harness exited 1"):
        run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    report.write_text(json.dumps({"resolved_ids": ["x__x-1"]}))
    evaluator(0, None)  # a clean exit that wrote no report: the one on disk is still the earlier one
    with pytest.raises(RuntimeError, match="no harness report from this evaluation"):
        run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    own = {"submitted_ids": ["x__x-1"], "resolved_ids": []}
    evaluator(0, own)  # the control: this evaluation's own report, under the same name
    assert run_swe.oracle(preds, "r1", ["x__x-1"], 1) == own


def test_an_overlapping_evaluations_report_under_the_same_run_id_is_not_this_ones(tmp_path, monkeypatch):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"instance_id": "y__y-2", "model_patch": "+y = 2\n"}) + "\n")

    def run(argv, *, capture_output, text, cwd, timeout):
        # another evaluation of other predictions under the same run id lands its report while this one
        # runs, named as a harness handed the bare run id names it; this one writes nothing
        (tmp_path / "claude-code-naked-m.r1.json").write_text(json.dumps({"submitted_ids": ["x__x-1"]}))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_swe.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="no harness report from this evaluation"):
        run_swe.oracle(preds, "r1", ["y__y-2"], 1)


def test_the_harness_grades_the_bytes_the_digest_names_whatever_rewrites_the_predictions(
    tmp_path, monkeypatch
):
    preds = tmp_path / "predictions.jsonl"
    first = json.dumps({"instance_id": "x__x-1", "model_patch": "+x = 1\n"}) + "\n"
    preds.write_text(first)
    handed: list[str] = []

    def run(argv, *, capture_output, text, cwd, timeout):
        # an overlapping run under the same run id rewrites the predictions before the harness reads
        preds.write_text(json.dumps({"instance_id": "x__x-1", "model_patch": "+x = 2\n"}) + "\n")
        path = Path(argv[argv.index("--predictions_path") + 1])
        handed.append(path.read_text())
        rid = argv[argv.index("--run_id") + 1]
        (Path(cwd) / f"m.{rid}.json").write_text(
            json.dumps({"submitted_ids": ["x__x-1"], "resolved_ids": []})
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_swe.subprocess, "run", run)
    run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    assert handed == [first]  # the patch the digest names, not the rewrite


def test_the_snapshot_the_harness_reads_is_whole_or_absent(tmp_path, monkeypatch):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"instance_id": "x__x-1", "model_patch": "+x = 1\n"}) + "\n")
    digest = hashlib.sha256(preds.read_bytes()).hexdigest()[:16]
    snapshot = tmp_path / f"predictions.{digest}.jsonl"
    real = Path.write_bytes

    def cut(self, data):  # the write dies half way: whatever path it was writing holds half the bytes
        real(self, data[: len(data) // 2])
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", cut)
    with pytest.raises(OSError, match="disk full"):
        run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    assert not snapshot.exists()  # never a half-written snapshot under the name the harness reads
    assert list(tmp_path.glob("*.part")) == []  # and no scratch file left behind
    # the move fails: the snapshot is untouched, the scratch file gone, the harness never called
    monkeypatch.setattr(Path, "write_bytes", real)
    snapshot.write_bytes(b"earlier, whole\n")
    calls: list = []

    def refuse(*a):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(run_swe.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(OSError, match="replace failed"):
        run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    assert (
        snapshot.read_bytes() == b"earlier, whole\n" and list(tmp_path.glob("*.part")) == [] and calls == []
    )
    # the scratch file cannot be removed either: the first fault is what the caller sees, noted
    unlink = Path.unlink

    def denied(self, missing_ok=False):
        if self.name.endswith(".part"):
            raise PermissionError("cleanup denied")
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(OSError, match="replace failed") as caught:
        run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    assert caught.value.__notes__ == [
        f"the scratch file {snapshot.name}.{os.getpid()}.part could not be removed: "
        "PermissionError: cleanup denied"
    ]
    assert calls == [] and snapshot.read_bytes() == b"earlier, whole\n"


def test_an_earlier_report_is_known_by_its_digest_and_not_held_through_the_evaluation(tmp_path, monkeypatch):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"instance_id": "x__x-1", "model_patch": "+x = 2\n"}) + "\n")
    digest = hashlib.sha256(preds.read_bytes()).hexdigest()[:16]
    report = tmp_path / f"claude-code-naked-m.r1.{digest}.json"
    earlier = json.dumps({"submitted_ids": ["x__x-1"], "resolved_ids": ["x__x-1"], "pad": "p" * 4000})
    report.write_text(earlier)
    held: list[str] = []

    def run(argv, *, capture_output, text, cwd, timeout):
        frame = sys._getframe(1)
        assert frame.f_code.co_name == "oracle"
        held.extend(k for k, v in frame.f_locals.items() if isinstance(v, bytes) and len(v) >= len(earlier))
        held.extend(f"before.{k}" for k, v in frame.f_locals["before"].items() if bytes in map(type, v))
        # rewritten within the same nanosecond as the earlier report: a different report all the same
        stat = report.stat()
        report.write_text(json.dumps(own))
        os.utime(report, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return subprocess.CompletedProcess(argv, 0, "", "")

    own = {"submitted_ids": ["x__x-1"], "resolved_ids": [], "pad": "q" * 4000}
    monkeypatch.setattr(run_swe.subprocess, "run", run)
    assert run_swe.oracle(preds, "r1", ["x__x-1"], 1) == own
    assert held == []


@pytest.mark.parametrize("streams", [(b"so far\n", b"slow\n"), ("so far\n", "slow\n"), (None, None)])
def test_an_evaluation_that_times_out_leaves_its_log_and_the_timeout_stands(tmp_path, monkeypatch, streams):
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"instance_id": "x__x-1", "model_patch": "+x = 2\n"}) + "\n")

    def run(argv, *, capture_output, text, cwd, timeout):
        raise subprocess.TimeoutExpired(argv, timeout, output=streams[0], stderr=streams[1])

    monkeypatch.setattr(run_swe.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        run_swe.oracle(preds, "r1", ["x__x-1"], 1)
    so_far, slow = ("so far\n", "slow\n") if streams[0] else ("", "")
    assert (tmp_path / "oracle_r1.log").read_text() == f"{so_far}\n--- stderr ---\n{slow}"
