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
from dataclasses import dataclass
from pathlib import Path

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


async def test_flow_checkpoint_checks_the_stringified_dataclass(tmp_path):
    from lionagi.cli.orchestrate._checkpoint import CheckpointWriter

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


def test_job_record_refuses_non_finite(tmp_path, monkeypatch):
    """``job.json`` is an open-shaped durable record like the rest.

    ``pid_create_time`` is the field that would carry one: it holds a process
    start time, and a start time that could not be read is recorded as ``null``,
    never as a non-finite float. So nothing here encodes a sentinel the refusal
    would destroy, and a NaN reaching this file is damage rather than meaning.
    """
    from lionagi.mcp import config, jobs

    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    run_id = jobs.new_run_id()
    target = config.job_dir(run_id) / "job.json"

    with pytest.raises(ValueError, match=r"non-finite float at \$\.pid_create_time"):
        jobs._write_job({"run_id": run_id, "pid": 4242, "pid_create_time": float("nan")})
    assert not target.exists()
    # And no staging file was left behind by the refusal either.
    assert not list(config.job_dir(run_id).glob(".job.json.*.tmp"))

    jobs._write_job({"run_id": run_id, "pid": 4242, "pid_create_time": None, "payload": CONTROL})
    _assert_control_roundtrip(target)
    assert json.loads(target.read_text())["pid_create_time"] is None


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_mcp_config_read_refuses_non_standard_json_constants(tmp_path, token):
    """The three tokens cannot enter the resolved server map at all.

    ``json.loads`` accepts them by default — they are a Python extension, not
    JSON — so a config carrying one would otherwise resolve to a Python float and
    be re-emitted into the snapshot the child is handed. The refusal names the
    file an operator wrote, and happens before anything is spawned.
    """
    from lionagi.cli._mcp_resolve import McpConfigError, resolve_spawn_mcp_servers

    (tmp_path / ".mcp.json").write_text(
        f'{{"mcpServers": {{"x": {{"command": "y", "timeout": {token}}}}}}}'
    )

    resolution = resolve_spawn_mcp_servers(launch_dir=tmp_path)
    assert resolution.servers is None
    assert resolution.reason.startswith("mcp_config_unusable:")
    assert token in resolution.reason

    with pytest.raises(McpConfigError, match="not valid JSON"):
        resolve_spawn_mcp_servers(tmp_path / ".mcp.json", launch_dir=tmp_path)

    # Control: the same config with a finite timeout resolves and keeps its value.
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"x": {"command": "y", "timeout": 1.5, "retries": null}}}'
    )
    ok = resolve_spawn_mcp_servers(launch_dir=tmp_path)
    assert ok.servers == {"x": {"command": "y", "timeout": 1.5, "retries": None}}


def test_mcp_server_snapshot_refuses_non_finite(tmp_path):
    """And the write refuses too, for a server map that reached it some other way."""
    from lionagi.mcp.jobs import _write_mcp_server_snapshot

    target = tmp_path / "mcp-servers.json"

    with pytest.raises(ValueError, match=r"non-finite float at \$\.mcpServers\.x\.timeout"):
        _write_mcp_server_snapshot(target, {"x": {"command": "y", "timeout": float("inf")}})
    assert not target.exists()

    _write_mcp_server_snapshot(target, {"payload": CONTROL})
    assert json.loads(target.read_text())["mcpServers"]["payload"] == CONTROL


def test_mirror_offsets_refuse_non_finite(tmp_path, monkeypatch):
    """The transcript cursor file is not the closed shape its fields suggest.

    The offset is this module's own arithmetic, but the tool-name map comes out
    of a transcript another program wrote and is never coerced to ``str``.
    """
    from lionagi.cli import mirror

    target = tmp_path / "offsets.json"
    monkeypatch.setattr(mirror, "_OFFSETS_PATH", target)

    bad = mirror._FileState(session_uid="s", offset=12, tool_names={"t1": float("nan")})
    with pytest.raises(ValueError, match=r"non-finite float at \$\.f\.tool_names\.t1"):
        mirror._save_states({"f": bad})
    assert not target.exists()

    good = mirror._FileState(session_uid="s", offset=12, tool_names={"t1": "Read"})
    mirror._save_states({"f": good})
    assert json.loads(target.read_text())["f"] == {
        "offset": 12,
        "session_uid": "s",
        "tool_names": {"t1": "Read"},
        "leaf_uuid": None,
    }


def test_hypothesis_chains_export_refuses_non_finite(tmp_path):
    """The chains artifact reaches the guard before ``chains.json`` is created."""
    from lionagi.engines.hypothesis import HypothesisEngine, HypothesisRun

    run = HypothesisRun(HypothesisEngine())
    run.root = "r"
    run.agents_made = float("inf")

    with pytest.raises(ValueError, match=r"non-finite float at \$\.agents_made"):
        run.export(tmp_path)
    assert not (tmp_path / "chains.json").exists()

    run.agents_made = 3
    paths = run.export(tmp_path)
    written = json.loads(Path(paths["chains"]).read_text())
    assert written["agents_made"] == 3
    assert written["root"] == "r"
    assert written["events"] == [] and written["open_questions"] == []


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
