# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resource-limited checkpoint decoding is visible at each reader surface.

The nesting case injects a decoder error; it does not measure the interpreter's
actual depth boundary or change the process recursion limit.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

import pytest

RUN = "checkpoint-resource-surfaces"
GEN = "0123456789abcdef0123456789abcdef"
SECRET = "private-decoder-input-sentinel"


def _deny(*_args, **_kwargs):
    raise AssertionError("this test cannot connect, launch a process, or send a signal")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _deny)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _deny)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _deny)


def _resource_document(version: int, failure: str) -> str:
    if failure == "nesting":
        value = "[0]"
    else:
        getter = getattr(sys, "get_int_max_str_digits", None)
        limit = getter() if getter is not None else 0
        if not limit or limit > 10_000:
            pytest.skip("integer digit limit is disabled or exceeds the bounded test input")
        value = "9" * max(5000, limit + 1)
    return f'{{"version":{version},"prompt":"{SECRET}","extra":{value}}}'


def _write_unit(root, base_text: str):
    directory = root / RUN
    directory.mkdir()
    path = directory / "checkpoint.json"
    path.write_text(base_text, encoding="utf-8")
    (directory / "checkpoint.json.journal").write_text("", encoding="utf-8")
    return path


def _assert_safe_chain(error: BaseException) -> None:
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered = str(current) + repr(current) + repr(current.args)
        assert SECRET not in rendered
        assert "9" * 200 not in rendered
        assert "line " not in rendered.lower()
        assert "column " not in rendered.lower()
        assert "char " not in rendered.lower()
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _assert_report(report):
    assert report["source_version"] is None
    assert report["disposition"] == "refused"
    assert report["valid_prefix_bytes"] is None
    assert report["problem"] == {
        "code": "invalid_record",
        "line": None,
        "byte_offset": None,
        "expected_seq": None,
        "actual_seq": None,
    }
    assert SECRET not in json.dumps(report)


@pytest.fixture
def _surface_stubs(tmp_path, monkeypatch):
    from lionagi import _paths
    from lionagi.cli.orchestrate import _checkpoint
    from lionagi.state import db as state_db
    from lionagi.studio.services import run_resume, sessions

    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(_paths, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(run_resume, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(run_resume, "read_only_open_supported", lambda: True)

    class FakeStudioDB:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_session(self, _run_id):
            return {"invocation_kind": "flow"}

    class FakeMcpDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_sessions_for_run(self, _run_id):
            return [{"id": "session", "updated_at": 1}]

    async def fake_session(_session_id):
        return {"graph": {"nodes": [{"id": "worker"}]}, "branches": [], "segments": []}

    monkeypatch.setattr(run_resume, "StateDB", FakeStudioDB)
    monkeypatch.setattr(state_db, "StateDB", FakeMcpDB)
    monkeypatch.setattr(sessions, "get_session", fake_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [2, 3], ids=["v2-shaped", "v3-shaped"])
@pytest.mark.parametrize("failure", ["nesting", "integer"])
async def test_resource_refusal_is_sanitized_on_all_surfaces(
    tmp_path, monkeypatch, _surface_stubs, version, failure
):
    from lionagi import _checkpoint_v3_reader as reader
    from lionagi.cli.orchestrate import _checkpoint, flow
    from lionagi.studio.services import run_resume
    from lionhub.serve.mcp._run_detail import _spawned_by_map, build_run_detail

    _write_unit(tmp_path, _resource_document(version, failure))
    injected = []
    if failure == "nesting":
        real_loads = json.loads

        def bounded_decoder_fault(value, *args, **kwargs):
            if (
                isinstance(value, str)
                and SECRET in value
                and '"extra"' in value
                and kwargs.get("object_pairs_hook") is reader._ObjectPairs
            ):
                injected.append(True)
                raise RecursionError(SECRET)
            return real_loads(value, *args, **kwargs)

        monkeypatch.setattr(reader.json, "loads", bounded_decoder_fault)
    with pytest.raises(_checkpoint.FlowCheckpointRecoveryError) as exc:
        await flow._resume_flow(RUN)
    if failure == "nesting":
        assert len(injected) == 1  # CLI reached the dispatch decoder.
    _assert_report(exc.value.checkpoint_recovery)
    _assert_safe_chain(exc.value)
    assert str(exc.value) == ("nesting depth" if failure == "nesting" else "integer digit limit")

    studio = await run_resume.resume_availability(RUN)
    if failure == "nesting":
        assert len(injected) == 2  # Studio preflight reached the same decoder.
    assert studio["resumable"] is False
    assert studio["reason"] == "invalid_checkpoint"
    _assert_report(studio["checkpoint_recovery"])
    assert SECRET not in json.dumps(studio)
    with pytest.raises(run_resume.RunResumeCheckpointError) as studio_exc:
        await run_resume._resolve_flow_checkpoint(RUN)
    _assert_safe_chain(studio_exc.value)
    if failure == "nesting":
        assert len(injected) == 3

    mcp = await build_run_detail(RUN)
    if failure == "nesting":
        assert len(injected) == 4  # MCP detail reached the same decoder.
    assert mcp["detail_unavailable"] == "checkpoint_recovery_refused"
    _assert_report(mcp["checkpoint_recovery"])
    assert SECRET not in json.dumps(mcp)
    with pytest.raises(reader.CheckpointRecoveryError) as mcp_exc:
        _spawned_by_map(RUN)
    _assert_safe_chain(mcp_exc.value)
    if failure == "nesting":
        assert len(injected) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [2, 3], ids=["v2", "v3"])
async def test_valid_checkpoint_control_is_readable_on_all_surfaces(
    tmp_path, _surface_stubs, version
):
    from lionagi.cli.orchestrate import _checkpoint
    from lionagi.studio.services import run_resume
    from lionhub.serve.mcp._run_detail import build_run_detail

    base = {
        "version": version,
        "session_id": "session",
        "prompt": "ordinary",
        "plan": [{"agent_id": "worker", "dep_indices": []}],
        "config": {},
        "flow_context": {},
        "ops": {},
        "spawned": [],
    }
    if version == 3:
        base.update(generation=GEN, journal_seq=0)
    path = _write_unit(tmp_path, json.dumps(base))
    assert _checkpoint.load_checkpoint(path)["version"] == version
    studio = await run_resume.resume_availability(RUN)
    assert studio["resumable"] is (version == 2)
    mcp = await build_run_detail(RUN)
    assert "detail_unavailable" not in mcp
    if version == 3:
        assert studio["reason"] == "v3_writer_unavailable"
        assert mcp["checkpoint_recovery"]["disposition"] == "clean"
    else:
        assert "checkpoint_recovery" not in studio
        assert "checkpoint_recovery" not in mcp
