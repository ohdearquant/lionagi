# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Checkpoint reader and writer version contracts."

from __future__ import annotations

import json

import pytest

from lionhub.orchestration.flow.checkpoint import (
    CheckpointWriter,
    FlowResumeError,
    load_checkpoint,
    resolve_checkpoint_target,
)
from tests._checkpoint_reader_versions_test_support import _RUN_ID as _RUN_ID
from tests._checkpoint_reader_versions_test_support import _base as _base
from tests._checkpoint_reader_versions_test_support import _write_fixture as _write_fixture
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


@pytest.mark.parametrize("version", [1, 2])
def test_loader_preserves_legacy_base_with_an_unrelated_journal(tmp_path, version):
    base = _base(version)
    path = _write_fixture(tmp_path, json.dumps(base))
    assert load_checkpoint(path) == base
    path.with_name("checkpoint.json.journal").write_text('{"seq":2}\n')
    assert load_checkpoint(path) == base


@pytest.mark.parametrize("version", [3, 999])
def test_loader_rejects_unsupported_base_with_journal(tmp_path, version):
    path = _write_fixture(tmp_path, json.dumps(_base(version)))
    with pytest.raises(FlowResumeError, match="not supported"):
        load_checkpoint(path)


def test_writer_version_uses_shared_reader_contract():
    import lionagi._checkpoint_version as checkpoint_version
    from lionhub.orchestration.flow.checkpoint import CHECKPOINT_VERSION

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
    import lionhub.orchestration.flow.checkpoint as checkpoint_mod

    _write_fixture(tmp_path, json.dumps(_base(3)))
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", tmp_path)
    with pytest.raises(FlowResumeError):
        await resolve_checkpoint_target(_RUN_ID)


@pytest.mark.asyncio
async def test_session_resolution_rejects_unsupported_version(tmp_path, monkeypatch):
    import lionagi.state.db as state_db
    import lionhub.orchestration.flow.checkpoint as checkpoint_mod
    import lionhub.orchestration.inspection as inspection_mod

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
    monkeypatch.setattr(inspection_mod, "resolve_any_target", resolve_any)
    monkeypatch.setattr(inspection_mod, "resolve_primary_session", resolve_primary)
    with pytest.raises(FlowResumeError):
        await resolve_checkpoint_target("session-id")


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
