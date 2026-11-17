# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""The MCP resume alias uses the branch of the exact owned agent job."""

from __future__ import annotations

import asyncio
import json

import pytest

from lionhub.serve.mcp import _notify_hook, config, dispatch, jobs

_FIRST_RUN = "20261116T110011-aaaaaa"
_SECOND_RUN = "20261116T110011-bbbbbb"
_FIRST_BRANCH = "aaaaaaaa-0000-4000-8000-000000000001"
_SECOND_BRANCH = "bbbbbbbb-0000-4000-8000-000000000002"


def _write(path, record):
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _save_branch(run_id, branch_id):
    _write(config.run_dir(run_id) / "branches" / f"{branch_id}.json", {"id": branch_id})


def _seed(run_id, branch_id, *, kind="agent", save=True):
    _write(
        config.job_dir(run_id) / "job.json", {"run_id": run_id, "kind": kind, "status": "running"}
    )
    _write(config.run_manifest(run_id), {"run_id": run_id, "branch_id": branch_id})
    if save:
        _save_branch(run_id, branch_id)


@pytest.fixture
def home_and_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _: None)
    monkeypatch.chdir(tmp_path)
    spawns = []

    def submit(kind, flags, **kwargs):
        spawns.append((kind, flags, kwargs))
        return {"run_id": "20261116T110012-cccccc"}

    monkeypatch.setattr(jobs, "submit", submit)
    return spawns


def _submit(**args):
    schema = asyncio.run(dispatch.request(help="agent.submit"))
    op = {
        "op": "agent.submit",
        "args": {"prompt": "continue the synthetic work", **args},
        "schema_fingerprint": schema["schema_fingerprint"],
    }
    return asyncio.run(dispatch.request(ops=[op]))["ops"][0]


def _status(run_id):
    api = asyncio.run(dispatch.request(ops=[{"op": "job.status", "args": {"run_id": run_id}}]))
    return api["ops"][0]["result"]


def test_fresh_handle_is_available_without_a_branch(home_and_spawns):
    result = _submit(query=["codex/gpt-5.3-codex"])

    assert result["ok"], result
    assert result["result"]["run_id"]
    assert result["result"]["resume_id"] is None
    assert len(home_and_spawns) == 1


def test_two_open_agent_runs_resume_only_the_requested_branch(home_and_spawns):
    _seed(_FIRST_RUN, _FIRST_BRANCH)
    _seed(_SECOND_RUN, _SECOND_BRANCH)

    for run_id, branch_id in ((_SECOND_RUN, _SECOND_BRANCH), (_FIRST_RUN, _FIRST_BRANCH)):
        result = _submit(resume=run_id)
        assert result["ok"], result
        assert result["result"]["resume_id"] == branch_id
        kind, argv, kwargs = home_and_spawns[-1]
        assert kind == "agent"
        assert [flag for flag in argv if flag.startswith("--resume=")] == [f"--resume={run_id}"]
        assert kwargs["replay_mcp_choice"] is True
        assert kwargs["write_capable"] is True


@pytest.mark.parametrize("resume", [_FIRST_BRANCH, _FIRST_BRANCH[:8], "20261116"])
def test_legacy_branch_id_or_prefix_is_unchanged(home_and_spawns, resume):
    result = _submit(resume=resume)

    assert result["ok"], result
    assert result["result"]["resume_id"] is None
    assert f"--resume={resume}" in home_and_spawns[0][1]


def test_run_id_prefix_refuses_without_a_job_read(home_and_spawns, monkeypatch):
    _seed(_FIRST_RUN, _FIRST_BRANCH)

    def unexpected_read(run_id, **kwargs):
        pytest.fail(f"the prefix was used for a job read: {run_id}")

    monkeypatch.setattr(jobs, "_resolved_run_state_dir", unexpected_read)
    result = _submit(resume=_FIRST_RUN[:-2])

    assert result["error"]["kind"] == "invalid_input"
    assert "full run ID" in result["error"]["message"]
    assert home_and_spawns == []


@pytest.mark.parametrize(
    "defect",
    [
        "missing-job",
        "wrong-job-id",
        "wrong-kind",
        "missing-manifest",
        "wrong-manifest-id",
        "nonobject-manifest",
        "oversize-manifest",
        "invalid-branch-id",
        "missing-snapshot",
        "snapshot-directory",
    ],
)
def test_run_resume_requires_the_owned_record_manifest_and_saved_branch(home_and_spawns, defect):
    _seed(_FIRST_RUN, _FIRST_BRANCH)
    job = config.job_dir(_FIRST_RUN) / "job.json"
    manifest = config.run_manifest(_FIRST_RUN)
    snapshot = config.run_dir(_FIRST_RUN) / "branches" / f"{_FIRST_BRANCH}.json"
    if defect == "missing-job":
        job.unlink()
    elif defect == "wrong-job-id":
        _write(job, {"run_id": _SECOND_RUN, "kind": "agent"})
    elif defect == "wrong-kind":
        _write(job, {"run_id": _FIRST_RUN, "kind": "flow"})
    elif defect == "missing-manifest":
        manifest.unlink()
    elif defect == "wrong-manifest-id":
        _write(manifest, {"run_id": _SECOND_RUN, "branch_id": _FIRST_BRANCH})
    elif defect == "nonobject-manifest":
        _write(manifest, [])
    elif defect == "oversize-manifest":
        _write(
            manifest,
            {"run_id": _FIRST_RUN, "branch_id": _FIRST_BRANCH, "extra": "x" * 1_048_576},
        )
    elif defect == "invalid-branch-id":
        _write(manifest, {"run_id": _FIRST_RUN, "branch_id": _FIRST_BRANCH[:8]})
    elif defect == "missing-snapshot":
        snapshot.unlink()
    elif defect == "snapshot-directory":
        snapshot.unlink()
        snapshot.mkdir()

    result = _submit(resume=_FIRST_RUN)

    assert result["error"]["kind"] == "invalid_input"
    assert home_and_spawns == []


def test_run_resume_ignores_job_records_in_a_different_home(home_and_spawns, tmp_path, monkeypatch):
    with monkeypatch.context() as foreign:
        foreign.setattr(config, "JOBS_DIR", tmp_path / "foreign" / "jobs")
        foreign.setattr("lionhub.orchestration.runs.RUNS_ROOT", tmp_path / "foreign" / "runs")
        _seed(_FIRST_RUN, _FIRST_BRANCH)
    _write(config.run_manifest(_FIRST_RUN), {"run_id": _FIRST_RUN, "branch_id": _SECOND_BRANCH})
    _write(
        config.run_dir(_FIRST_RUN) / "branches" / f"{_SECOND_BRANCH}.json",
        {"id": _SECOND_BRANCH},
    )

    result = _submit(resume=_FIRST_RUN)

    assert result["error"]["kind"] == "invalid_input"
    assert home_and_spawns == []


@pytest.mark.parametrize("entry", ["job", "run-dir", "manifest", "branch-dir", "snapshot"])
def test_run_resume_refuses_links_in_its_verified_path(home_and_spawns, entry):
    _seed(_FIRST_RUN, _FIRST_BRANCH)
    run_dir = config.run_dir(_FIRST_RUN)
    branch_dir = run_dir / "branches"
    path = {
        "job": config.job_dir(_FIRST_RUN) / "job.json",
        "run-dir": run_dir,
        "manifest": config.run_manifest(_FIRST_RUN),
        "branch-dir": branch_dir,
        "snapshot": branch_dir / f"{_FIRST_BRANCH}.json",
    }[entry]
    is_dir = path.is_dir()
    renamed = path.rename(path.with_name(path.name + ".backup"))
    path.symlink_to(renamed, target_is_directory=is_dir)

    result = _submit(resume=_FIRST_RUN)

    assert result["error"]["kind"] == "invalid_input"
    assert home_and_spawns == []


def test_agent_status_exposes_only_the_branch_in_its_owned_manifest(home_and_spawns):
    _write(
        config.job_dir(_FIRST_RUN) / "job.json",
        {"run_id": _FIRST_RUN, "kind": "agent", "status": "running"},
    )
    assert _status(_FIRST_RUN)["resume_id"] is None

    _write(config.run_manifest(_FIRST_RUN), {"run_id": _SECOND_RUN, "branch_id": _SECOND_BRANCH})
    assert _status(_FIRST_RUN)["resume_id"] is None

    _seed(_FIRST_RUN, _FIRST_BRANCH)
    assert _status(_FIRST_RUN)["resume_id"] == _FIRST_BRANCH

    _seed(_SECOND_RUN, _SECOND_BRANCH, kind="flow")
    assert "resume_id" not in _status(_SECOND_RUN)


def _notice_resume_id(monkeypatch, run_id):
    delivered = []

    def delivery(argv, payload, *args, **kwargs):
        delivered.append(payload)
        return {"attempted": True, "ok": True}

    with monkeypatch.context() as notice:
        notice.setattr(_notify_hook, "_resolve_command", lambda *a, **k: (["notifier"], None))
        notice.setattr(_notify_hook, "_resolve_delivery_cwd", lambda *a: (None, None))
        notice.setattr(_notify_hook, "_deliver", delivery)
        job = jobs._read_job(run_id)
        result = _notify_hook.deliver_terminal_notice(run_id, job, "completed", target="test")
    assert result["ok"]
    (payload,) = delivered
    return payload["resume_id"]


def test_agent_terminal_notice_without_a_manifest_carries_null(home_and_spawns, monkeypatch):
    _write(config.job_dir(_FIRST_RUN) / "job.json", {"run_id": _FIRST_RUN, "kind": "agent"})
    assert _notice_resume_id(monkeypatch, _FIRST_RUN) is None


def test_agent_resume_id_tracks_the_saved_branch(home_and_spawns, monkeypatch):
    _seed(_FIRST_RUN, _FIRST_BRANCH, save=False)

    refused = _submit(resume=_FIRST_RUN)
    assert refused["error"]["kind"] == "invalid_input"
    assert home_and_spawns == []
    projected = (_status(_FIRST_RUN)["resume_id"], _notice_resume_id(monkeypatch, _FIRST_RUN))
    assert projected == (None, None)

    _save_branch(_FIRST_RUN, _FIRST_BRANCH)

    assert _status(_FIRST_RUN)["resume_id"] == _FIRST_BRANCH
    assert _notice_resume_id(monkeypatch, _FIRST_RUN) == _FIRST_BRANCH
    resumed = _submit(resume=_FIRST_RUN)
    assert resumed["ok"], resumed
    assert resumed["result"]["resume_id"] == _FIRST_BRANCH
    ((kind, argv, _),) = home_and_spawns
    assert kind == "agent"
    assert [flag for flag in argv if flag.startswith("--resume=")] == [f"--resume={_FIRST_RUN}"]
