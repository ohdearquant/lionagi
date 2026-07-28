# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Every durable JSON file lionagi writes refuses a non-finite float.

A NaN or an Infinity has no JSON representation. The stdlib writes the tokens
``NaN``/``Infinity``, which Python itself reads back, so the file looks fine to
the process that produced it and breaks for every strict reader; orjson writes
``null``, which nothing can tell apart from a value that was genuinely null.
Either way the loss is durable and invisible where it is created, so each writer
refuses at the write and names the offending field path.

Each test carries its own positive control: the same writer, a payload holding a
legitimate ``None`` and ordinary floats, which must still be written and read
back unchanged. Without it a writer that refused everything would pass.
"""

import json

import pytest

CONTROL = {"finite": 1.5, "negative": -0.25, "zero": 0.0, "absent": None}


def _assert_control_roundtrip(path):
    """The control payload survived the write byte-for-value."""
    loaded = json.loads(path.read_text())
    assert loaded["payload"] == CONTROL


def test_run_manifest_writer_refuses_non_finite(tmp_path):
    from lionagi.cli._runs import _atomic_write_json

    target = tmp_path / "run.json"

    with pytest.raises(ValueError, match=r"non-finite float at \$\.usage\.cost"):
        _atomic_write_json(target, {"usage": {"cost": float("inf")}})
    assert not target.exists()

    _atomic_write_json(target, {"payload": CONTROL})
    _assert_control_roundtrip(target)


def test_orchestration_branch_snapshot_refuses_non_finite(tmp_path):
    from lionagi.cli.orchestrate._orchestration import _write_branch_snapshot

    class _Branch:
        def __init__(self, dict_):
            self._dict = dict_

        def to_dict(self):
            return self._dict

    target = tmp_path / "branch.json"

    with pytest.raises(ValueError, match=r"non-finite float at \$\.metrics\.latency"):
        _write_branch_snapshot(target, _Branch({"metrics": {"latency": float("nan")}}))
    assert not target.exists()

    _write_branch_snapshot(target, _Branch({"payload": CONTROL}))
    _assert_control_roundtrip(target)


async def test_flow_checkpoint_refuses_non_finite(tmp_path):
    from lionagi.cli.orchestrate._checkpoint import CheckpointWriter

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


def test_team_inbox_refuses_non_finite(tmp_path):
    from lionagi.cli.team import _locked_team

    target = tmp_path / "team.json"

    with pytest.raises(ValueError, match=r"non-finite float at \$\.messages\[0\]\.weight"):
        with _locked_team("t", create_path=target) as data:
            data["messages"] = [{"weight": float("nan")}]
    # The lock is released and the file left as it was, not truncated.
    assert target.read_text() == ""

    with _locked_team("t", create_path=target) as data:
        data["payload"] = CONTROL
    _assert_control_roundtrip(target)


def test_coding_measurements_refuse_non_finite():
    from lionagi.engines.coding import CodeResultRecorded, CodingRun

    class _Run(CodingRun):
        def __init__(self, measurements):  # bypass the Engine wiring
            self.store = {
                CodeResultRecorded: [
                    CodeResultRecorded(eid="K1", passed=True, measurements=measurements)
                ]
            }

    with pytest.raises(ValueError, match=r"non-finite float at \$\.pass_rate"):
        _Run({"pass_rate": float("nan")}).to_hypothesis_seeds()

    seeds = _Run(dict(CONTROL)).to_hypothesis_seeds()
    assert json.loads(seeds[0]["measurements"]) == CONTROL


async def test_run_branch_snapshot_refuses_non_finite(tmp_path):
    from uuid import uuid4

    from lionagi.operations.run.run import _write_branch_snapshot

    class _Branch:
        def __init__(self, dict_):
            self.id = uuid4()
            self._dict = dict_

        def to_dict(self):
            return self._dict

    bad = _Branch({"usage": {"cost": float("inf")}})
    with pytest.raises(ValueError, match=r"non-finite float at \$\.usage\.cost"):
        await _write_branch_snapshot(bad, tmp_path)
    assert not (tmp_path / f"{bad.id}.json").exists()

    good = _Branch({"payload": CONTROL})
    await _write_branch_snapshot(good, tmp_path)
    _assert_control_roundtrip(tmp_path / f"{good.id}.json")


async def test_stream_buffer_chunk_refuses_non_finite(tmp_path):
    from lionagi.operations.run.run import _append_chunk

    class _Chunk:
        def __init__(self, dict_):
            self._dict = dict_

        def to_dict(self):
            return self._dict

    target = tmp_path / "b.jsonl"

    with pytest.raises(ValueError, match=r"non-finite float at \$\.usage\.tokens_per_s"):
        await _append_chunk(target, _Chunk({"usage": {"tokens_per_s": float("nan")}}))
    assert not target.exists()

    await _append_chunk(target, _Chunk({"payload": CONTROL}))
    assert json.loads(target.read_text())["payload"] == CONTROL
