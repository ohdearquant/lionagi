# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Exercise checkpoint version guards with synthetic journals.

The current writer emits no journal. Legacy fixtures include one only to
characterize base-only reads; unsupported bases must be refused.
"""

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


@pytest.mark.asyncio
async def test_cli_resume_rejects_unsupported_checkpoint_before_flow(tmp_path, monkeypatch):
    import lionhub.orchestration.flow.checkpoint as checkpoint_mod
    import lionhub.serve.cli.orchestrate.flow as flow_mod

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
    if version != 2:
        assert detail == {"detail_unavailable": "unsupported_checkpoint_version"}
    else:
        assert {node["id"]: node["spawned_by"] for node in detail["nodes"]} == {
            "base-child": "base-parent",
            "journal-child": None,
        }
        (tmp_path / _RUN_ID / "checkpoint.json.journal").write_text('{"seq":2}\n')
        assert await build_run_detail(_RUN_ID) == detail
