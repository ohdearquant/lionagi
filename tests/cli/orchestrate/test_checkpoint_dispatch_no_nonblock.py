# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint dispatch when the platform does not expose O_NONBLOCK."""

import json
import os
from pathlib import Path

import pytest

from lionagi import _checkpoint_v3_reader as reader

GENERATION = "0123456789abcdef0123456789abcdef"
RUN = "no-nonblock-legacy-run"


class _NoNonblockOS:
    """Hide the flag at the reader seam without mutating the process-wide os module."""

    def __init__(self):
        self.open_flags = []
        self.fstat_calls = 0

    def __getattr__(self, name):
        if name == "O_NONBLOCK":
            raise AttributeError(name)
        return getattr(os, name)

    def open(self, path, flags, *args, **kwargs):
        self.open_flags.append(flags)
        # The exercised paths are regular files or a directory, never FIFOs.
        # On POSIX, keep the actual call nonblocking as an extra test safeguard.
        return os.open(path, flags | getattr(os, "O_NONBLOCK", 0), *args, **kwargs)

    def fstat(self, fd):
        self.fstat_calls += 1
        return os.fstat(fd)


def _checkpoint(root: Path, version: int) -> tuple[Path, dict]:
    directory = root / RUN
    directory.mkdir()
    state = {
        "version": version,
        "session_id": "session",
        "prompt": "example",
        "plan": [],
        "config": {"no_mcp_config": True},
        "flow_context": {"values": []},
        "ops": {},
        "spawned": [],
    }
    if version == 3:
        state.update(generation=GENERATION, journal_seq=0)
        journal = {
            "version": 3,
            "generation": GENERATION,
            "seq": 1,
            "kind": "op",
            "entry": {"agent_id": "op", "status": "completed", "response": "ok"},
            "flow_context": {"patch": [{"op": "add", "path": "/values/-", "value": 1}]},
        }
        (directory / "checkpoint.json.journal").write_text(json.dumps(journal) + "\n")
    path = directory / "checkpoint.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path, state


@pytest.mark.parametrize("version", [2, 3], ids=["v2", "v3"])
def test_dispatch_without_nonblocking_flag_loads_regular_checkpoint(tmp_path, monkeypatch, version):
    path, state = _checkpoint(tmp_path, version)
    os_proxy = _NoNonblockOS()
    monkeypatch.setattr(reader, "os", os_proxy)
    loaded, report = reader.load_checkpoint_unit(path, retries=1)
    assert report["source_version"] == version
    assert report["disposition"] == "clean"
    assert os_proxy.open_flags and all(flags == os.O_RDONLY for flags in os_proxy.open_flags)
    assert os_proxy.fstat_calls >= 2
    if version == 2:
        assert loaded == state
    else:
        assert loaded["ops"]["op"]["response"] == "ok"
        assert loaded["flow_context"] == {"values": [1]}
        assert report["applied_records"] == 1


@pytest.mark.asyncio
async def test_legacy_v2_can_resume_without_nonblocking_flag(tmp_path, monkeypatch):
    from lionagi.cli.orchestrate import _checkpoint as checkpoint_module
    from lionagi.cli.orchestrate import flow

    _checkpoint(tmp_path, 2)
    os_proxy = _NoNonblockOS()
    monkeypatch.setattr(reader, "os", os_proxy)
    monkeypatch.setattr(checkpoint_module, "RUNS_ROOT", tmp_path)
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "ok", "completed"

    monkeypatch.setattr(flow, "_run_flow", fake_run)
    assert await flow._resume_flow(RUN) == ("ok", "completed")
    assert calls[0]["resume_checkpoint"]["version"] == 2
    assert os_proxy.open_flags and all(flags == os.O_RDONLY for flags in os_proxy.open_flags)


def test_directory_is_refused_without_nonblocking_flag(tmp_path, monkeypatch):
    directory = tmp_path / "directory-checkpoint"
    directory.mkdir()
    os_proxy = _NoNonblockOS()
    monkeypatch.setattr(reader, "os", os_proxy)
    with pytest.raises(reader.CheckpointRecoveryError) as caught:
        reader.load_checkpoint_unit(directory, retries=1)
    assert caught.value.checkpoint_recovery["problem"]["code"] == "incoherent_view"
    assert os_proxy.open_flags == [os.O_RDONLY]
