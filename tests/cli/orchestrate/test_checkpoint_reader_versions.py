# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Exercise checkpoint version guards with synthetic journals.

The current writer emits no journal. Legacy fixtures include one only to
characterize base-only reads; unsupported bases must be refused.
"""

from __future__ import annotations

import json

import pytest

from lionagi.cli.orchestrate._checkpoint import (
    CheckpointWriter,
    FlowResumeError,
    load_checkpoint,
    resolve_checkpoint_target,
)

_RUN_ID = "synthetic-run"


def _base(version):
    return {
        "version": version,
        "generation": "0123456789abcdef0123456789abcdef",
        "journal_seq": 0,
        "session_id": "synthetic-session",
        "prompt": "synthetic",
        "plan": [],
        "config": {},
        "flow_context": {"nested": {"entries": []}},
        "ops": {},
        "spawned": [{"node_id": "base-child", "parent_id": "base-parent"}],
    }


def _write_fixture(root, text):
    directory = root / _RUN_ID
    directory.mkdir()
    checkpoint = directory / "checkpoint.json"
    checkpoint.write_text(text, encoding="utf-8")
    (directory / "checkpoint.json.journal").write_text(
        '{"seq":1,"entry":{"node_id":"journal-child","parent_id":"journal-parent"}}\n',
        encoding="utf-8",
    )
    return checkpoint


@pytest.mark.parametrize("version", [1, 2])
def test_loader_preserves_legacy_base_with_an_unrelated_journal(tmp_path, version):
    base = _base(version)
    path = _write_fixture(tmp_path, json.dumps(base))
    assert load_checkpoint(path) == base
    path.with_name("checkpoint.json.journal").write_text('{"seq":2}\n')
    assert load_checkpoint(path) == base


def test_loader_rejects_unsupported_base_with_journal(tmp_path):
    path = _write_fixture(tmp_path, json.dumps(_base(999)))
    with pytest.raises(FlowResumeError, match="not supported"):
        load_checkpoint(path)


def test_loader_refuses_malformed_v3_journal(tmp_path):
    path = _write_fixture(tmp_path, json.dumps(_base(3)))
    with pytest.raises(FlowResumeError) as exc:
        load_checkpoint(path)
    assert exc.value.checkpoint_recovery["disposition"] == "refused"


def test_writer_version_uses_shared_reader_contract():
    import lionagi._checkpoint_version as checkpoint_version
    from lionagi.cli.orchestrate._checkpoint import CHECKPOINT_VERSION

    assert CHECKPOINT_VERSION == checkpoint_version.CHECKPOINT_VERSION == 2
    assert CHECKPOINT_VERSION in checkpoint_version._SUPPORTED_CHECKPOINT_VERSIONS


@pytest.mark.parametrize("version", ["null", "true", '"2"', "2.0", "0", "-1"])
def test_loader_rejects_nonstandard_version_values(tmp_path, version):
    path = _write_fixture(tmp_path, '{"version":' + version + "}")
    with pytest.raises(FlowResumeError, match="not supported"):
        load_checkpoint(path)


@pytest.mark.parametrize(
    "versions",
    [
        '"version":3,"version":2',
        '"version":2,"version":3',
        '"version":3,"vers\\u0069on":2',
        '"version":2,"vers\\u0069on":3',
        '"version":2,"version":2',
    ],
)
def test_loader_rejects_ambiguous_top_level_version(tmp_path, versions):
    path = _write_fixture(tmp_path, "{" + versions + ' ,"prompt":"private payload"}')
    with pytest.raises(FlowResumeError, match="ambiguous") as exc:
        load_checkpoint(path)
    assert "private payload" not in str(exc.value)


def test_legacy_missing_version_and_nested_duplicate_version_remain_accepted(tmp_path):
    path = _write_fixture(tmp_path, '{"nested":{"version":3,"version":2},"prompt":"ok"}')
    assert load_checkpoint(path) == {"nested": {"version": 2}, "prompt": "ok"}


def test_nested_duplicate_version_does_not_make_the_root_ambiguous(tmp_path):
    path = _write_fixture(
        tmp_path,
        '{"version":2,"nested":{"version":3,"version":2},"items":[{"version":4,"version":1}]}',
    )
    assert load_checkpoint(path) == {
        "version": 2,
        "nested": {"version": 2},
        "items": [{"version": 1}],
    }


def test_nested_unambiguous_version_does_not_hide_ambiguous_root(tmp_path):
    path = _write_fixture(tmp_path, '{"version":3,"vers\\u0069on":2,"nested":{"version":1}}')
    with pytest.raises(FlowResumeError, match="ambiguous"):
        load_checkpoint(path)


def test_malformed_json_remains_a_decode_error(tmp_path):
    path = _write_fixture(tmp_path, '{"version":2,')
    with pytest.raises(json.JSONDecodeError):
        load_checkpoint(path)


@pytest.mark.asyncio
async def test_direct_run_resolution_rejects_unsupported_version(tmp_path, monkeypatch):
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    _write_fixture(tmp_path, json.dumps(_base(3)))
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", tmp_path)
    with pytest.raises(FlowResumeError):
        await resolve_checkpoint_target(_RUN_ID)


@pytest.mark.asyncio
async def test_session_resolution_rejects_unsupported_version(tmp_path, monkeypatch):
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod
    import lionagi.cli.status as status_mod
    import lionagi.state.db as state_db

    _write_fixture(tmp_path, json.dumps(_base(3)))
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", tmp_path)

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def resolve_any(_db, target):
        assert target == "session-id"
        return "session", {"id": "session-id"}

    async def resolve_primary(_db, entity_type, _row):
        assert entity_type == "session"
        return {"id": "session-id", "node_metadata": {"run_id": _RUN_ID}}

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(status_mod, "_resolve_any_target", resolve_any)
    monkeypatch.setattr(status_mod, "_resolve_primary_session", resolve_primary)
    with pytest.raises(FlowResumeError):
        await resolve_checkpoint_target("session-id")


@pytest.mark.asyncio
async def test_cli_resume_rejects_unsupported_checkpoint_before_flow(tmp_path, monkeypatch):
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod
    import lionagi.cli.orchestrate.flow as flow_mod

    _write_fixture(tmp_path, json.dumps(_base(3)))
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", tmp_path)
    called = False

    async def run_flow(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unsupported checkpoint reached flow execution")

    monkeypatch.setattr(flow_mod, "_run_flow", run_flow)
    with pytest.raises(FlowResumeError):
        await flow_mod._resume_flow(_RUN_ID)
    assert not called


@pytest.mark.asyncio
async def test_writer_still_emits_v2_bytes(tmp_path):
    path = tmp_path / "checkpoint.json"
    writer = CheckpointWriter(path, "s", "p", [], {})
    await writer.record("worker", status="completed", response="hello", flow_context={"x": [1]})
    assert path.read_text() == json.dumps(
        {
            "version": 2,
            "session_id": "s",
            "prompt": "p",
            "plan": [],
            "flow_context": {"x": [1]},
            "ops": {"worker": {"agent_id": "worker", "status": "completed", "response": "hello"}},
            "spawned": [],
            "config": {},
        },
        default=str,
    )
    assert json.loads(path.read_text())["version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [2, 3, 999, "duplicate"])
async def test_mcp_detail_reports_unsupported_checkpoint(tmp_path, monkeypatch, version):
    from lionagi import _paths
    from lionagi.state import db as state_db
    from lionagi.studio.services import sessions as sessions_svc
    from lionhub.serve.mcp._run_detail import build_run_detail

    if version == "duplicate":
        contents = '{"version":3,"vers\\u0069on":2,"spawned":[]}'
    else:
        contents = json.dumps(_base(version))
    _write_fixture(tmp_path, contents)
    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)

    class FakeStateDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_sessions_for_run(self, run_id):
            assert run_id == _RUN_ID
            return [{"id": "synthetic-session", "updated_at": 1}]

    async def get_session(session_id):
        assert session_id == "synthetic-session"
        return {
            "graph": {"nodes": [{"id": "base-child"}, {"id": "journal-child"}]},
            "branches": [],
            "segments": [],
        }

    monkeypatch.setattr(state_db, "StateDB", FakeStateDB)
    monkeypatch.setattr(sessions_svc, "get_session", get_session)
    detail = await build_run_detail(_RUN_ID)
    if version == 3:
        assert detail["detail_unavailable"] == "checkpoint_recovery_refused"
        assert detail["checkpoint_recovery"]["disposition"] == "refused"
    elif version != 2:
        assert detail == {"detail_unavailable": "unsupported_checkpoint_version"}
    else:
        assert {node["id"]: node["spawned_by"] for node in detail["nodes"]} == {
            "base-child": "base-parent",
            "journal-child": None,
        }
        (tmp_path / _RUN_ID / "checkpoint.json.journal").write_text('{"seq":2}\n')
        assert await build_run_detail(_RUN_ID) == detail
