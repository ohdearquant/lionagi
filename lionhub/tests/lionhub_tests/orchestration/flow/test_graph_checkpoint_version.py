# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Version 4 is a written graph's checkpoint, and version and graph record agree at read."""

from __future__ import annotations

import json

import pytest

import lionhub.orchestration.flow.checkpoint_version as checkpoint_version
from lionhub.orchestration.flow.checkpoint import (
    CheckpointWriter,
    UnsupportedFlowCheckpointVersionError,
    load_checkpoint,
)

_RECORD = {"sha256": "0" * 64, "path": "/work/g.json", "resolved": "/work/g.json"}


def _checkpoint(version, config) -> dict:
    checkpoint = {"session_id": "s", "prompt": "", "plan": [], "ops": {}, "config": config}
    if version is not None:
        checkpoint["version"] = version
    return checkpoint


async def test_a_written_graph_checkpoint_is_version_4_and_a_planned_one_stays_2(tmp_path):
    graph_path = tmp_path / "graph" / "checkpoint.json"
    await CheckpointWriter(graph_path, "s", "", [], {"graph": dict(_RECORD)}).flush()
    planned_path = tmp_path / "planned" / "checkpoint.json"
    await CheckpointWriter(planned_path, "s", "p", [], {"model_spec": "m"}).flush()

    assert json.loads(graph_path.read_text())["version"] == 4
    assert load_checkpoint(graph_path)["config"]["graph"] == _RECORD
    assert json.loads(planned_path.read_text())["version"] == 2


@pytest.mark.parametrize(
    ("version", "config"),
    [
        (4, {}),
        (4, {"model_spec": "m"}),
        (4, None),
        (2, {"graph": _RECORD}),
        (1, {"graph": _RECORD}),
        (None, {"graph": _RECORD}),
        # Version 3 is the journaled format, not read yet, with or without a graph record.
        (3, {"graph": _RECORD}),
        (3, {}),
    ],
)
def test_version_and_graph_record_must_agree(tmp_path, version, config):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(_checkpoint(version, config)))
    with pytest.raises(UnsupportedFlowCheckpointVersionError, match="not supported"):
        load_checkpoint(path)


@pytest.mark.parametrize(
    ("version", "config"), [(4, {"graph": _RECORD}), (2, {}), (1, {}), (None, {})]
)
def test_agreeing_checkpoints_load(tmp_path, version, config):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(_checkpoint(version, config)))
    assert load_checkpoint(path)["config"] == config


def test_both_writer_versions_are_readable_and_3_is_not():
    assert checkpoint_version.CHECKPOINT_VERSION == 2
    assert checkpoint_version.GRAPH_CHECKPOINT_VERSION == 4
    assert checkpoint_version._SUPPORTED_CHECKPOINT_VERSIONS == {1, 2, 4}


@pytest.mark.parametrize("writers", [(2, 3), (3, 4), (0, 4)])
def test_the_import_time_check_fails_for_either_writer_outside_the_set(writers):
    with pytest.raises(RuntimeError, match="writer version is not supported"):
        checkpoint_version._check_writer_versions(
            writers, checkpoint_version._SUPPORTED_CHECKPOINT_VERSIONS
        )
