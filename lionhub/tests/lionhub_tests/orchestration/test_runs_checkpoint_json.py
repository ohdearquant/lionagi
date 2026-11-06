# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Run and checkpoint writers reject non-finite payloads."

import json
from dataclasses import dataclass

import pytest

from tests._runs_checkpoint_json_test_support import CONTROL as CONTROL
from tests._runs_checkpoint_json_test_support import (
    _assert_control_roundtrip as _assert_control_roundtrip,
)


def test_run_manifest_writer_refuses_non_finite(tmp_path):
    from lionhub.orchestration.runs import _atomic_write_json

    target = tmp_path / "run.json"

    with pytest.raises(ValueError, match=r"non-finite float at \$\.usage\.cost"):
        _atomic_write_json(target, {"usage": {"cost": float("inf")}})
    assert not target.exists()

    _atomic_write_json(target, {"payload": CONTROL})
    _assert_control_roundtrip(target)


async def test_flow_checkpoint_refuses_non_finite(tmp_path):
    from lionhub.orchestration.flow.checkpoint import CheckpointWriter

    def _writer(ops):
        return CheckpointWriter(
            path=tmp_path / "checkpoint.json",
            session_id="s",
            prompt="p",
            plan=[],
            config={},
            ops=ops,
        )

    bad = _writer({"a": {"agent_id": "a", "status": "done", "response": {"score": float("-inf")}}})
    with pytest.raises(ValueError, match=r"non-finite float at \$\.ops\.a\.response\.score"):
        await bad.flush()
    assert not (tmp_path / "checkpoint.json").exists()

    good = _writer({"a": {"agent_id": "a", "status": "done", "response": CONTROL}})
    await good.flush()
    written = json.loads((tmp_path / "checkpoint.json").read_text())
    assert written["ops"]["a"]["response"] == CONTROL


async def test_flow_checkpoint_checks_the_stringified_dataclass(tmp_path):
    from lionhub.orchestration.flow.checkpoint import CheckpointWriter

    @dataclass
    class Response:
        score: float

    response = Response(score=float("nan"))
    writer = CheckpointWriter(
        path=tmp_path / "checkpoint.json",
        session_id="s",
        prompt="p",
        plan=[],
        config={},
        ops={"a": {"agent_id": "a", "status": "done", "response": response}},
    )

    await writer.flush()

    written = json.loads((tmp_path / "checkpoint.json").read_text())
    assert written["ops"]["a"]["response"] == str(response)
