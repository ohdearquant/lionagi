# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint recovery through CLI, Studio preflight, and MCP detail."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from argparse import Namespace
from types import SimpleNamespace

import pytest

RUN = "checkpoint-surface-run"
GEN = "0123456789abcdef0123456789abcdef"
SECRET = "sensitive-context-sentinel"


def _deny(*_args, **_kwargs):
    raise AssertionError("this test must not start processes, signal, or use sockets")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _deny)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _deny)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _deny)


def _fixture(root, *, invalid=False, torn=False):
    directory = root / RUN
    directory.mkdir()
    base = {
        "version": 3,
        "generation": GEN,
        "journal_seq": 0,
        "session_id": "source-session",
        "prompt": SECRET,
        "plan": [{"agent_id": "worker", "dep_indices": []}],
        "config": {"no_mcp_config": True},
        "flow_context": {"entries": []},
        "ops": {},
        "spawned": [],
    }
    record = {
        "version": 3,
        "generation": GEN,
        "seq": 1,
        "kind": "spawned",
        "entry": {
            "node_id": "child",
            "parent_id": "parent",
            "status": "completed",
            "response": SECRET,
            "operation": "work",
        },
        "flow_context": {"patch": [{"op": "add", "path": "/entries/-", "value": "x"}]},
    }
    if invalid:
        record["flow_context"]["patch"].append({"op": "remove", "path": "/missing"})
    path = directory / "checkpoint.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    journal = json.dumps(record, separators=(",", ":")) + "\n"
    if torn:
        journal += '{"version":3'
    (directory / "checkpoint.json.journal").write_text(journal, encoding="utf-8")
    return path


def test_cli_loader_reads_journal_and_keeps_report_outside_state(tmp_path):
    from lionagi.cli.orchestrate._checkpoint import load_checkpoint

    state = load_checkpoint(_fixture(tmp_path, torn=True))
    assert state["spawned"][0]["node_id"] == "child"
    assert state["flow_context"] == {"entries": ["x"]}
    assert state.checkpoint_recovery["disposition"] == "recovered_prefix"
    assert state.checkpoint_recovery["ignored"]["torn_final_record"] == 1
    assert "checkpoint_recovery" not in state
    assert "checkpoint_recovery" not in json.loads(json.dumps(state))


@pytest.mark.asyncio
async def test_cli_warns_and_refuses_v3_before_execution(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint, flow

    path = _fixture(tmp_path, torn=True)
    journal = path.with_name("checkpoint.json.journal")
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, journal)]
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(flow, "_warn", lambda message: calls.append(("warning", message)))

    async def fake_run_flow(**kwargs):
        calls.append(("execute", kwargs))
        return "done", "completed"

    monkeypatch.setattr(flow, "_run_flow", fake_run_flow)
    with pytest.raises(_checkpoint.FlowV3WriterUnavailableError) as exc:
        await flow._resume_flow(RUN, output_format="json")
    assert exc.value.reason == "v3_writer_unavailable"
    assert exc.value.checkpoint_recovery["disposition"] == "recovered_prefix"
    assert [item[0] for item in calls] == ["warning"]
    assert SECRET not in calls[0][1]
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, journal)] == before


def test_cli_entrypoint_refuses_v3_without_changing_checkpoint(tmp_path, monkeypatch, capsys):
    from lionagi.cli import orchestrate
    from lionagi.cli.orchestrate import _checkpoint, flow

    path = _fixture(tmp_path, torn=True)
    journal = path.with_name("checkpoint.json.journal")
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, journal)]
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    errors = []
    monkeypatch.setattr(orchestrate, "log_error", errors.append)

    async def forbidden_setup(**_kwargs):
        pytest.fail("v3 resume reached setup before writer availability")

    monkeypatch.setattr(flow, "setup_orchestration", forbidden_setup)
    code = orchestrate.run_orchestrate(
        Namespace(orch_command="flow", resume=RUN, dry_run=False, verbose=False, output="json")
    )
    assert code == orchestrate.EXIT_CODE_BY_STATUS["failed"]
    assert len(errors) == 1 and "v3_writer_unavailable" in errors[0]
    assert SECRET not in errors[0]
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "v3_writer_unavailable"
    assert result["checkpoint_recovery"]["disposition"] == "recovered_prefix"
    assert SECRET not in json.dumps(result)
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, journal)] == before


@pytest.mark.asyncio
async def test_direct_run_flow_refuses_v3_before_setup(tmp_path, monkeypatch):
    from lionagi._checkpoint_v3_reader import CheckpointRecoveryError
    from lionagi.cli.orchestrate import _checkpoint, flow

    checkpoint = _checkpoint.load_checkpoint(_fixture(tmp_path))

    async def forbidden_setup(**_kwargs):
        pytest.fail("v3 resume reached setup before writer availability")

    monkeypatch.setattr(flow, "setup_orchestration", forbidden_setup)
    with pytest.raises(_checkpoint.FlowV3WriterUnavailableError) as exc:
        await flow._run_flow("fake", "prompt", resume_checkpoint=checkpoint)
    assert exc.value.reason == "v3_writer_unavailable"
    assert isinstance(exc.value, _checkpoint.FlowResumeError)
    assert isinstance(exc.value, CheckpointRecoveryError)
    assert exc.value.checkpoint_recovery == checkpoint.checkpoint_recovery
    assert exc.value.checkpoint_recovery["disposition"] == "clean"
    assert exc.value.checkpoint_recovery["problem"] is None


@pytest.mark.asyncio
async def test_v2_resume_remains_available_for_cli_and_studio(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint, flow
    from lionagi.studio.services import run_resume

    path = _fixture(tmp_path)
    base = json.loads(path.read_text())
    base["version"] = 2
    path.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "ok", "completed"

    monkeypatch.setattr(flow, "_run_flow", fake_run)
    assert await flow._resume_flow(RUN) == ("ok", "completed")
    assert calls[0]["resume_checkpoint"]["version"] == 2
    assert "checkpoint_recovery" not in calls[0]

    monkeypatch.setattr(
        run_resume._subprocess, "resolve_li_executable", lambda: (["fake-li"], None)
    )

    async def fake_active(_run_id):
        return None

    async def fake_launch(_argv, **_kwargs):
        return "fake-invocation"

    monkeypatch.setattr(run_resume, "_active_flow_resume", fake_active)
    monkeypatch.setattr(run_resume._launches, "launch_detached_argv", fake_launch)
    result = await run_resume._resume_flow_run(
        RUN, invocation_kind="flow", allow_degraded_context=False, retry_failed=False
    )
    assert result["invocation_id"] == "fake-invocation"
    assert "checkpoint_recovery" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_run_flow_returns_json_report_and_stamps_metadata(tmp_path, monkeypatch, dry_run):
    from lionagi.cli.orchestrate import _checkpoint, flow
    from lionagi.state.lifecycle import notify_settings

    checkpoint = _checkpoint.load_checkpoint(_fixture(tmp_path, torn=True))
    report = checkpoint.checkpoint_recovery
    env = SimpleNamespace(
        run=SimpleNamespace(run_id="new-run", artifact_root=tmp_path),
        session=SimpleNamespace(id="new-session", branches=[]),
        default_model_spec="fake-model",
        orc_profile=None,
        orc_profile_name=None,
        _live_persist=None,
    )
    seen = []

    async def fake_setup(**_kwargs):
        return env

    async def fake_start(_env, **kwargs):
        seen.append(kwargs["extra_node_metadata"])

    async def fake_stop(_env, *, status):
        return status

    async def fake_inner(*_args, **_kwargs):
        return "Plan text" if dry_run else json.dumps({"workers": [{"response": "ok"}]})

    monkeypatch.setattr(flow, "setup_orchestration", fake_setup)
    monkeypatch.setattr(flow, "start_live_persist", fake_start)
    monkeypatch.setattr(flow, "stop_live_persist", fake_stop)
    monkeypatch.setattr(flow, "_run_flow_inner", fake_inner)
    monkeypatch.setattr(flow, "parse_orchestrator_provider", lambda _name: ("fake", "fake"))
    monkeypatch.setattr(flow, "unregister_flow_notify_scope", lambda *_args: None)
    monkeypatch.setattr(
        notify_settings, "register_run_notify_outcome_scope", lambda *_args, **_kw: None
    )
    monkeypatch.setattr(notify_settings, "unregister_run_notify_outcome_scope", lambda *_args: None)

    output, status = await flow._run_flow(
        "fake-model",
        "prompt",
        output_format="json",
        dry_run=dry_run,
        resume_checkpoint={**checkpoint, "version": 2},
        checkpoint_recovery=report,
    )
    assert status == "completed"
    payload = json.loads(output)
    assert payload["checkpoint_recovery"] == report
    assert seen[0]["checkpoint_recovery"] == report
    assert seen[0]["resumed_from"] == "source-session"
    assert "checkpoint_recovery" not in checkpoint
    if dry_run:
        assert payload["plan"] == "Plan text"
    else:
        assert payload["workers"] == [{"response": "ok"}]


@pytest.mark.asyncio
async def test_invalid_report_never_reaches_graph_or_warning(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint, flow

    checkpoint = _checkpoint.load_checkpoint(_fixture(tmp_path, torn=True))
    checkpoint.checkpoint_recovery = {"disposition": SECRET}

    async def fake_resolve(_target):
        return SimpleNamespace(), checkpoint

    async def forbidden_run(**_kwargs):
        pytest.fail("invalid report reached graph execution")

    warnings = []
    monkeypatch.setattr(flow, "resolve_checkpoint_target", fake_resolve)
    monkeypatch.setattr(flow, "_run_flow", forbidden_run)
    monkeypatch.setattr(flow, "_warn", warnings.append)
    with pytest.raises(_checkpoint.FlowResumeError) as exc:
        await flow._resume_flow(RUN)
    assert SECRET not in str(exc.value)
    assert not warnings


@pytest.mark.asyncio
async def test_null_or_unrecognized_warning_reason_is_redacted(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint, flow

    checkpoint = _checkpoint.load_checkpoint(_fixture(tmp_path, torn=True))
    warnings = []

    async def fake_resolve(_target):
        return SimpleNamespace(), checkpoint

    async def fake_run(**_kwargs):
        return "ok", "completed"

    monkeypatch.setattr(flow, "resolve_checkpoint_target", fake_resolve)
    monkeypatch.setattr(flow, "_run_flow", fake_run)
    monkeypatch.setattr(flow, "_warn", warnings.append)
    for problem in (None, {"code": SECRET}):
        checkpoint.checkpoint_recovery["problem"] = problem
        with pytest.raises(_checkpoint.FlowV3WriterUnavailableError):
            await flow._resume_flow(RUN)
    assert len(warnings) == 2
    assert all(SECRET not in warning and "recovered_prefix" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_cli_refuses_complete_invalid_v3_before_execution(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint, flow

    _fixture(tmp_path, invalid=True)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)

    async def forbidden_run_flow(**_kwargs):
        pytest.fail("invalid checkpoint reached graph execution")

    monkeypatch.setattr(flow, "_run_flow", forbidden_run_flow)
    with pytest.raises(_checkpoint.FlowCheckpointRecoveryError) as exc:
        await flow._resume_flow(RUN)
    assert exc.value.checkpoint_recovery["disposition"] == "refused"
    assert SECRET not in str(exc.value)
    assert SECRET not in json.dumps(exc.value.checkpoint_recovery)


def test_malformed_base_retains_json_error_and_sanitized_report(tmp_path):
    from lionagi.cli.orchestrate import _checkpoint

    path = _fixture(tmp_path)
    path.write_text('{\n"prompt":"' + SECRET + '",\n"version":}', encoding="utf-8")
    with pytest.raises(_checkpoint.FlowCheckpointDecodeError) as exc:
        _checkpoint.load_checkpoint(path)
    assert isinstance(exc.value, json.JSONDecodeError)
    assert exc.value.checkpoint_recovery["problem"]["code"] == "invalid_record"
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(exc.value)
    assert SECRET not in exc.value.doc
    assert SECRET not in json.dumps(exc.value.checkpoint_recovery)
    assert exc.value.lineno == 3
    assert f"line {exc.value.lineno} column {exc.value.colno}" in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_utf8_refusal_is_compatible_and_sanitized(tmp_path, monkeypatch):
    from lionagi._checkpoint_v3_reader import CheckpointRecoveryError
    from lionagi.cli.orchestrate import _checkpoint, flow

    path = _fixture(tmp_path)
    prefix = b'{"version":3,"prompt":"' + SECRET.encode()
    path.write_bytes(prefix + b"\xff")
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)

    async def forbidden_run(**_kwargs):
        pytest.fail("invalid UTF-8 reached graph execution")

    monkeypatch.setattr(flow, "_run_flow", forbidden_run)
    with pytest.raises(UnicodeDecodeError) as exc:
        await flow._resume_flow(RUN)
    assert isinstance(exc.value, CheckpointRecoveryError)
    assert exc.value.start == len(prefix)
    assert exc.value.checkpoint_recovery["source_version"] is None
    assert exc.value.checkpoint_recovery["problem"]["byte_offset"] == len(prefix)
    assert exc.value.checkpoint_recovery["problem"]["line"] is None
    assert SECRET.encode() not in exc.value.object
    assert SECRET not in str(exc.value) + repr(exc.value) + repr(exc.value.args)


def test_cli_entrypoint_handles_invalid_utf8_without_traceback(tmp_path, monkeypatch):
    from lionagi.cli import orchestrate
    from lionagi.cli.orchestrate import _checkpoint

    path = _fixture(tmp_path)
    path.write_bytes(b'{"version":3,"prompt":"' + SECRET.encode() + b"\xff")
    with pytest.raises(UnicodeDecodeError) as caught:
        _checkpoint.load_checkpoint(path)
    errors = []

    def fake_run_async(coro):
        coro.close()
        raise caught.value

    monkeypatch.setattr(orchestrate, "run_async", fake_run_async)
    monkeypatch.setattr(orchestrate, "log_error", errors.append)
    code = orchestrate.run_orchestrate(
        Namespace(orch_command="flow", resume=RUN, dry_run=False, verbose=False, output="json")
    )
    assert code == orchestrate.EXIT_CODE_BY_STATUS["failed"]
    assert len(errors) == 1
    assert SECRET not in errors[0]


def test_cli_entrypoint_emits_sanitized_json_on_invalid_utf8(tmp_path, monkeypatch, capsys):
    from lionagi.cli import orchestrate
    from lionagi.cli.orchestrate import _checkpoint

    path = _fixture(tmp_path)
    prefix = b'{"version":3,"prompt":"' + SECRET.encode()
    path.write_bytes(prefix + b"\xff")
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    errors = []
    monkeypatch.setattr(orchestrate, "log_error", errors.append)
    code = orchestrate.run_orchestrate(
        Namespace(orch_command="flow", resume=RUN, dry_run=False, verbose=False, output="json")
    )
    result = json.loads(capsys.readouterr().out)
    assert code == orchestrate.EXIT_CODE_BY_STATUS["failed"]
    assert result["error"] == "checkpoint_recovery_refused"
    assert result["checkpoint_recovery"]["problem"]["byte_offset"] == len(prefix)
    assert SECRET not in json.dumps(result) + repr(errors)


@pytest.mark.asyncio
async def test_studio_preflight_reports_recovery_and_refusal(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint
    from lionagi.studio.services import run_resume

    path = _fixture(tmp_path, torn=True)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(run_resume, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(run_resume, "read_only_open_supported", lambda: True)

    class FakeStateDB:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_session(self, _run_id):
            return {"invocation_kind": "flow"}

    monkeypatch.setattr(run_resume, "StateDB", FakeStateDB)
    result = await run_resume.resume_availability(RUN)
    assert result["resumable"] is False
    assert result["reason"] == "v3_writer_unavailable"
    assert result["checkpoint_recovery"]["disposition"] == "recovered_prefix"

    base = json.loads(path.read_text())
    base["plan"] = []
    path.write_text(json.dumps(base), encoding="utf-8")
    empty = await run_resume.resume_availability(RUN)
    assert empty["resumable"] is False
    assert empty["reason"] == "v3_writer_unavailable"
    assert empty["checkpoint_recovery"]["disposition"] == "recovered_prefix"
    base["plan"] = [{"agent_id": "worker", "dep_indices": []}]
    path.write_text(json.dumps(base), encoding="utf-8")

    path.with_name("checkpoint.json.journal").write_text(
        json.dumps(
            {
                "version": 3,
                "generation": GEN,
                "seq": 2,
                "kind": "op",
                "entry": {"agent_id": "worker", "status": "completed", "response": SECRET},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = await run_resume.resume_availability(RUN)
    assert result["resumable"] is False
    assert result["reason"] == "invalid_checkpoint"
    assert result["checkpoint_recovery"]["problem"]["code"] == "invalid_sequence"
    assert SECRET not in json.dumps(result)
    path.write_text('{"version":3,"prompt":"' + SECRET, encoding="utf-8")
    malformed = await run_resume.resume_availability(RUN)
    assert malformed["resumable"] is False
    assert malformed["reason"] == "invalid_checkpoint"
    assert malformed["checkpoint_recovery"]["problem"]["code"] == "invalid_record"
    assert SECRET not in json.dumps(malformed)
    prefix = b'{"version":3,"prompt":"' + SECRET.encode()
    path.write_bytes(prefix + b"\xff")
    invalid_utf8 = await run_resume.resume_availability(RUN)
    assert invalid_utf8["resumable"] is False
    assert invalid_utf8["reason"] == "invalid_checkpoint"
    assert invalid_utf8["checkpoint_recovery"]["problem"]["byte_offset"] == len(prefix)
    assert invalid_utf8["checkpoint_recovery"]["problem"]["line"] is None
    assert SECRET not in json.dumps(invalid_utf8)


@pytest.mark.asyncio
async def test_studio_refuses_v3_without_launch_or_source_changes(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint
    from lionagi.studio.services import run_resume

    path = _fixture(tmp_path, torn=True)
    journal = path.with_name("checkpoint.json.journal")
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, journal)]
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(
        run_resume._subprocess, "resolve_li_executable", lambda: (["fake-li"], None)
    )
    captured = []

    async def fake_active(_run_id):
        return None

    async def fake_launch(argv, **kwargs):
        captured.append((argv, kwargs))
        return "invocation"

    monkeypatch.setattr(run_resume, "_active_flow_resume", fake_active)
    monkeypatch.setattr(run_resume._launches, "launch_detached_argv", fake_launch)
    with pytest.raises(run_resume.RunResumeCheckpointError) as exc:
        await run_resume._resume_flow_run(
            RUN, invocation_kind="flow", allow_degraded_context=False, retry_failed=False
        )
    assert exc.value.reason == "v3_writer_unavailable"
    assert exc.value.checkpoint_recovery["disposition"] == "recovered_prefix"
    assert captured == []
    assert SECRET not in json.dumps(exc.value.checkpoint_recovery)
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, journal)] == before


@pytest.mark.asyncio
async def test_mcp_uses_journal_spawn_and_refuses_invalid_record(tmp_path, monkeypatch):
    from lionagi import _paths
    from lionagi.state import db as state_db
    from lionagi.studio.services import sessions
    from lionhub.serve.mcp._run_detail import build_run_detail

    path = _fixture(tmp_path, torn=True)
    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_sessions_for_run(self, _run_id):
            return [{"id": "session", "updated_at": 1}]

    async def fake_session(_session_id):
        return {"graph": {"nodes": [{"id": "child"}]}, "branches": [], "segments": []}

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(sessions, "get_session", fake_session)
    detail = await build_run_detail(RUN)
    assert detail["nodes"][0]["spawned_by"] == "parent"
    assert detail["checkpoint_recovery"]["disposition"] == "recovered_prefix"
    assert detail["resume_unavailable_reason"] == "v3_writer_unavailable"

    path.with_name("checkpoint.json.journal").write_text(
        json.dumps(
            {
                "version": 3,
                "generation": GEN,
                "seq": 2,
                "kind": "op",
                "entry": {"agent_id": "worker", "status": "completed", "response": SECRET},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    refused = await build_run_detail(RUN)
    assert refused["detail_unavailable"] == "checkpoint_recovery_refused"
    assert refused["checkpoint_recovery_reason"] == "invalid_sequence"
    assert refused["checkpoint_recovery"]["problem"]["code"] == "invalid_sequence"
    assert refused["checkpoint_recovery"]["base_sequence"] == 0
    assert refused["checkpoint_recovery"]["last_applied_sequence"] == 0
    assert refused["checkpoint_recovery"]["valid_prefix_bytes"] == 0
    assert refused["checkpoint_recovery"]["problem"] == {
        "code": "invalid_sequence",
        "line": 1,
        "byte_offset": 0,
        "expected_seq": 1,
        "actual_seq": 2,
    }
    assert SECRET not in json.dumps(refused)
    path.write_text('{"version":3,"prompt":"' + SECRET, encoding="utf-8")
    malformed = await build_run_detail(RUN)
    assert malformed["detail_unavailable"] == "checkpoint_recovery_refused"
    assert malformed["checkpoint_recovery_reason"] == "invalid_record"
    assert malformed["checkpoint_recovery"]["problem"]["code"] == "invalid_record"
    assert malformed["checkpoint_recovery"]["problem"]["line"] == 1
    assert malformed["checkpoint_recovery"]["problem"]["byte_offset"] is not None
    assert SECRET not in json.dumps(malformed)
    prefix = b'{"version":3,"prompt":"' + SECRET.encode()
    path.write_bytes(prefix + b"\xff")
    invalid_utf8 = await build_run_detail(RUN)
    assert invalid_utf8["detail_unavailable"] == "checkpoint_recovery_refused"
    assert invalid_utf8["checkpoint_recovery_reason"] == "invalid_utf8"
    assert invalid_utf8["checkpoint_recovery"]["problem"]["byte_offset"] == len(prefix)
    assert invalid_utf8["checkpoint_recovery"]["problem"]["line"] is None
    assert SECRET not in json.dumps(invalid_utf8)


@pytest.mark.asyncio
async def test_mcp_no_graph_still_reports_v3_and_keeps_absent_case(tmp_path, monkeypatch):
    from lionagi import _paths
    from lionagi.state import db as state_db
    from lionagi.studio.services import sessions
    from lionhub.serve.mcp._run_detail import build_run_detail

    path = _fixture(tmp_path, torn=True)
    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_sessions_for_run(self, _run_id):
            return [{"id": "session", "updated_at": 1}]

    async def empty_session(_session_id):
        return {"graph": None, "branches": [], "node_metadata": None}

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(sessions, "get_session", empty_session)
    detail = await build_run_detail(RUN)
    assert detail["detail_unavailable"] == "no_session_detail"
    assert detail["resume_unavailable_reason"] == "v3_writer_unavailable"
    assert detail["checkpoint_recovery"]["disposition"] == "recovered_prefix"
    assert "nodes" not in detail
    assert await build_run_detail("absent-run") == {"detail_unavailable": "no_session_detail"}

    path.with_name("checkpoint.json.journal").write_text('{"version":3}\n', encoding="utf-8")
    refused = await build_run_detail(RUN)
    assert refused["detail_unavailable"] == "checkpoint_recovery_refused"
    assert refused["checkpoint_recovery"]["disposition"] == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("has_graph", [False, True], ids=["empty-session", "graph"])
@pytest.mark.parametrize("failure", ["json", "utf8"])
async def test_mcp_unclassifiable_malformed_base_has_reason_and_safe_positions(
    tmp_path, monkeypatch, version, has_graph, failure
):
    from lionagi import _paths
    from lionagi.state import db as state_db
    from lionagi.studio.services import sessions
    from lionhub.serve.mcp._run_detail import build_run_detail

    path = _fixture(tmp_path)
    prefix = f'{{"version":{version},"prompt":"{SECRET}'.encode()
    path.write_bytes(prefix + (b"\xff" if failure == "utf8" else b""))
    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_sessions_for_run(self, _run_id):
            return [{"id": "session", "updated_at": 1}]

    async def fake_session(_session_id):
        return {
            "graph": {"nodes": [{"id": "child"}]} if has_graph else None,
            "branches": [],
            "node_metadata": None,
        }

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(sessions, "get_session", fake_session)
    result = await build_run_detail(RUN)

    assert result["detail_unavailable"] == "checkpoint_recovery_refused"
    assert result["checkpoint_recovery_reason"] == (
        "invalid_utf8" if failure == "utf8" else "invalid_record"
    )
    problem = result["checkpoint_recovery"]["problem"]
    assert all(
        result["checkpoint_recovery"][key] is None
        for key in ("base_sequence", "last_applied_sequence", "valid_prefix_bytes")
    )
    assert problem["code"] == "invalid_record"
    assert problem["line"] == (1 if failure == "json" else None)
    expected_offset = (
        len(prefix) if failure == "utf8" else len(f'{{"version":{version},"prompt":'.encode())
    )
    assert problem["byte_offset"] == expected_offset
    assert problem["expected_seq"] is None
    assert problem["actual_seq"] is None
    assert SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_duplicate_journal_report_matches_cli_studio_and_mcp(tmp_path, monkeypatch):
    from lionagi import _paths
    from lionagi.cli.orchestrate import _checkpoint
    from lionagi.state import db as state_db
    from lionagi.studio.services import run_resume, sessions
    from lionhub.serve.mcp._run_detail import build_run_detail

    path = _fixture(tmp_path)
    journal = path.with_name("checkpoint.json.journal")
    first = journal.read_bytes()
    duplicate = (
        f'{{"version":3,"generation":"{GEN}","seq":2,"kind":"op",'
        f'"entry":{{"agent_id":"worker","agent_id":"duplicate",'
        f'"status":"completed","response":"{SECRET}"}}}}\n'
    ).encode()
    journal.write_bytes(first + duplicate)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_sessions_for_run(self, _run_id):
            return [{"id": "session", "updated_at": 1}]

    async def fake_session(_session_id):
        return {"graph": {"nodes": [{"id": "child"}]}, "branches": [], "segments": []}

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(sessions, "get_session", fake_session)

    with pytest.raises(_checkpoint.FlowCheckpointRecoveryError) as cli:
        _checkpoint.load_checkpoint(path)
    with pytest.raises(run_resume.RunResumeCheckpointError) as studio:
        await run_resume._resolve_flow_checkpoint(RUN)
    mcp = await build_run_detail(RUN)

    expected = cli.value.checkpoint_recovery
    assert studio.value.reason == "invalid_checkpoint"
    assert mcp["detail_unavailable"] == "checkpoint_recovery_refused"
    assert mcp["checkpoint_recovery_reason"] == "invalid_record"
    assert studio.value.checkpoint_recovery == mcp["checkpoint_recovery"] == expected
    assert expected["base_sequence"] == 0
    assert expected["last_applied_sequence"] == 1
    assert expected["applied_records"] == 1
    assert expected["valid_prefix_bytes"] == len(first)
    assert expected["problem"] == {
        "code": "invalid_record",
        "line": 2,
        "byte_offset": len(first),
        "expected_seq": None,
        "actual_seq": None,
    }
    assert SECRET not in json.dumps(mcp)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (42, 42), (None, None), (True, None), (-1, None), (1.0, None), (SECRET, None)],
    ids=["zero", "positive", "null", "bool", "negative", "float", "payload"],
)
async def test_mcp_refusal_only_retains_valid_numeric_positions(monkeypatch, value, expected):
    from lionagi._checkpoint_v3_reader import CheckpointRecoveryError, _report
    from lionagi.state import db as state_db
    from lionagi.studio.services import sessions
    from lionhub.serve.mcp import _run_detail

    report = _report()
    report.update(
        source_version=3,
        disposition="refused",
        base_sequence=value,
        last_applied_sequence=value,
        valid_prefix_bytes=value,
        problem={
            "code": "invalid_record",
            "line": value,
            "byte_offset": value,
            "expected_seq": value,
            "actual_seq": value,
        },
    )

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_sessions_for_run(self, _run_id):
            return [{"id": "session", "updated_at": 1}]

    async def fake_session(_session_id):
        return {"graph": {"nodes": [{"id": "child"}]}, "branches": [], "segments": []}

    def refuse(_run_id):
        raise CheckpointRecoveryError(report)

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(sessions, "get_session", fake_session)
    monkeypatch.setattr(_run_detail, "_spawned_by_map", refuse)
    result = await _run_detail.build_run_detail(RUN)

    assert result["detail_unavailable"] == "checkpoint_recovery_refused"
    safe = result["checkpoint_recovery"]
    assert all(
        safe[key] == expected
        for key in ("base_sequence", "last_applied_sequence", "valid_prefix_bytes")
    )
    assert all(
        safe["problem"][key] == expected
        for key in ("line", "byte_offset", "expected_seq", "actual_seq")
    )
    assert SECRET not in json.dumps(result)
