"""Exercise development matrix decisions with captured response shapes."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/dev_full_matrix_plan.py"
_planner = runpy.run_path(str(SCRIPT))
dev_sha = _planner["dev_sha"]
should_run = _planner["should_run"]

SHA = "1" * 40
OLDER_SHA = "2" * 40
PREFIX = "repos/ohdearquant/lionagi"
REF = f"{PREFIX}/git/ref/heads/dev/lionagi"
RUNS = (
    f"{PREFIX}/actions/workflows/dev-full-matrix.yml/runs?event=schedule&status=success&per_page=1"
)
JOBS = f"{PREFIX}/actions/runs/72/jobs?filter=latest&per_page=100"


def _ref(sha=SHA):
    return {"ref": "refs/heads/dev/lionagi", "object": {"sha": sha, "type": "commit"}}


def _runs(*ids, event="schedule", conclusion="success"):
    return {
        "workflow_runs": [
            {"id": run_id, "event": event, "conclusion": conclusion, "head_sha": OLDER_SHA}
            for run_id in ids
        ]
    }


def _certificate(sha=SHA, status="completed", conclusion="success"):
    return {
        "name": f"Full dev matrix passed {sha}",
        "status": status,
        "conclusion": conclusion,
    }


def _reader(*requests):
    outstanding = list(requests)

    def read(path):
        expected, response = outstanding.pop(0)
        assert path == expected
        return response

    return read


def test_the_first_schedule_runs_the_matrix():
    assert should_run("schedule", 73, SHA, _reader((f"{RUNS}&page=1", _runs())))


@pytest.mark.parametrize(
    ("jobs", "run"),
    [
        ([_certificate()], False),
        ([_certificate(OLDER_SHA)], True),
        ([_certificate(status="queued")], True),
        ([_certificate(conclusion="skipped")], True),
        ([{"name": f"Other job {SHA}", "status": "completed", "conclusion": "success"}], True),
        ([], True),
    ],
)
def test_only_a_successful_certificate_for_the_exact_sha_skips(jobs, run):
    read = _reader(
        (f"{RUNS}&page=1", _runs(72)),
        (f"{JOBS}&page=1", {"jobs": jobs}),
    )
    assert should_run("schedule", 73, SHA, read) is run


def test_skips_its_own_earlier_attempt_and_checks_the_previous_run():
    read = _reader(
        (f"{RUNS}&page=1", _runs(73)),
        (f"{RUNS}&page=2", _runs(72)),
        (f"{JOBS}&page=1", {"jobs": [_certificate()]}),
    )
    assert not should_run("schedule", 73, SHA, read)


def test_rerun_without_a_previous_run_cannot_use_its_own_certificate():
    read = _reader((f"{RUNS}&page=1", _runs(73)), (f"{RUNS}&page=2", _runs()))
    assert should_run("schedule", 73, SHA, read)


def test_reads_certificate_on_a_later_jobs_page():
    read = _reader(
        (f"{RUNS}&page=1", _runs(72)),
        (f"{JOBS}&page=1", {"jobs": [{"name": "Other job"}] * 100}),
        (f"{JOBS}&page=2", {"jobs": [_certificate()]}),
    )
    assert not should_run("schedule", 73, SHA, read)


@pytest.mark.parametrize(
    "response", [_ref("1" * 39), _ref("no dev SHA"), {"ref": "refs/heads/main"}]
)
def test_an_invalid_dev_ref_fails(response):
    with pytest.raises(ValueError, match="dev/lionagi"):
        dev_sha(_reader((REF, response)))


@pytest.mark.parametrize(
    "runs",
    [{}, _runs(72, event="workflow_dispatch"), _runs(72, conclusion="failure")],
)
def test_a_malformed_or_non_scheduled_success_response_fails(runs):
    with pytest.raises(ValueError, match="GitHub response"):
        should_run("schedule", 73, SHA, _reader((f"{RUNS}&page=1", runs)))


def test_a_malformed_jobs_response_fails():
    read = _reader((f"{RUNS}&page=1", _runs(72)), (f"{JOBS}&page=1", {}))
    with pytest.raises(ValueError, match="jobs list"):
        should_run("schedule", 73, SHA, read)


def test_cli_writes_outputs_for_manual_dispatch(monkeypatch, tmp_path):
    output = tmp_path / "output"
    output.write_text("earlier=value\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "workflow_dispatch", "73"])

    def gh_api(command, **_kwargs):
        assert command == ["gh", "api", "--hostname", "github.com", REF]
        return subprocess.CompletedProcess(command, 0, json.dumps(_ref()), "")

    monkeypatch.setattr(subprocess, "run", gh_api)
    runpy.run_path(str(SCRIPT), run_name="__main__")
    assert output.read_text() == f"earlier=value\nsha={SHA}\nrun=true\n"


def test_ref_api_failure_prevents_job_outputs(monkeypatch, tmp_path, capsys):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "schedule", "73"])

    def gh_api(command, **_kwargs):
        assert command == ["gh", "api", "--hostname", "github.com", REF]
        return subprocess.CompletedProcess(command, 1, "", "API unavailable")

    monkeypatch.setattr(subprocess, "run", gh_api)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 1
    assert "failed with exit 1" in capsys.readouterr().err
    assert not output.exists()


def test_history_api_failure_preserves_only_the_validated_sha(monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "schedule", "73"])

    def gh_api(command, **_kwargs):
        if command == ["gh", "api", "--hostname", "github.com", REF]:
            return subprocess.CompletedProcess(command, 0, json.dumps(_ref()), "")
        assert command == ["gh", "api", "--hostname", "github.com", f"{RUNS}&page=1"]
        assert output.read_text() == f"sha={SHA}\n"
        return subprocess.CompletedProcess(command, 1, "", "API unavailable")

    monkeypatch.setattr(subprocess, "run", gh_api)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 1
    assert output.read_text() == f"sha={SHA}\n"
