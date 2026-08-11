# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durability and write-amplification contracts for journaled flow checkpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from pathlib import Path

import pytest

from lionagi.cli.orchestrate._checkpoint import (
    CHECKPOINT_VERSION,
    CheckpointWriter,
    load_checkpoint,
)


def _writer(path: Path, *, compact_every: int = 128) -> CheckpointWriter:
    return CheckpointWriter(
        path=path,
        session_id="session-1",
        prompt="run the plan",
        plan=[],
        config={"model_spec": "codex"},
        version=CHECKPOINT_VERSION,
        compact_every=compact_every,
    )


async def test_journaled_checkpoint_recovers_concurrent_records_in_writer_order(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    await asyncio.gather(
        *(writer.record(f"agent-{i}", status="completed", response=i) for i in range(20))
    )

    recovered = load_checkpoint(path)
    assert list(recovered["ops"]) == [f"agent-{i}" for i in range(20)]
    assert [entry["seq"] for entry in writer.journal_records()] == list(range(1, 21))
    assert not list(tmp_path.glob("checkpoint.*.tmp"))


async def test_journaled_checkpoint_writes_linear_deltas_for_growing_context(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = _writer(path, compact_every=128)
    context: dict[str, str] = {}

    for i in range(200):
        context = {**context, f"context-{i}": "c" * 8_192}
        await writer.record(
            f"agent-{i}",
            status="completed",
            response="r" * 8_192,
            flow_context=context,
        )

    recovered = load_checkpoint(path)
    assert len(recovered["ops"]) == 200
    assert recovered["flow_context"] == context
    assert writer.bytes_written < 10_000_000


async def test_journaled_checkpoint_reports_and_ignores_torn_final_record(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-1", status="completed", response="durable")

    with writer.journal_path.open("ab") as stream:
        stream.write(b'{"generation":"torn')

    recovered = load_checkpoint(path)
    assert recovered["ops"]["agent-1"]["response"] == "durable"
    assert recovered["_recovery"]["torn_final_record"] is True


async def test_journaled_checkpoint_compaction_rebases_generation_atomically(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = _writer(path, compact_every=2)

    await writer.record("agent-1", status="completed", response="one")
    generation_before = json.loads(path.read_text())["generation"]
    await writer.record("agent-2", status="completed", response="two")

    base = json.loads(path.read_text())
    assert base["generation"] != generation_before
    assert base["ops"]["agent-2"]["response"] == "two"
    assert writer.journal_path.read_bytes() == b""
    assert load_checkpoint(path)["ops"] == writer.ops


async def test_journal_serialization_and_fsync_run_off_the_event_loop(tmp_path: Path, monkeypatch):
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.flush()
    started = threading.Event()
    release = threading.Event()
    real_append = checkpoint_mod._append_journal_record

    def slow_append(journal_path: Path, record: dict) -> int:
        started.set()
        release.wait(timeout=2)
        return real_append(journal_path, record)

    monkeypatch.setattr(checkpoint_mod, "_append_journal_record", slow_append)
    task = asyncio.create_task(writer.record("agent", status="completed", response="ok"))
    assert await asyncio.to_thread(started.wait, 1)

    ticked = False

    def tick() -> None:
        nonlocal ticked
        ticked = True

    asyncio.get_running_loop().call_soon(tick)
    await asyncio.sleep(0)
    assert ticked is True
    assert task.done() is False
    release.set()
    await task


async def test_journal_rejects_non_finite_delta_without_mutating_recovered_state(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    with pytest.raises(ValueError, match=r"non-finite float.*response.score"):
        await writer.record(
            "agent-bad",
            status="completed",
            response={"score": float("nan")},
        )

    assert "agent-bad" not in writer.ops
    assert "agent-bad" not in load_checkpoint(path)["ops"]


async def test_journal_context_delta_applies_deletions_and_none_values(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    await writer.record(
        "agent-1",
        status="completed",
        response="one",
        flow_context={"remove": "old", "nullable": "value"},
    )
    await writer.record(
        "agent-2",
        status="completed",
        response="two",
        flow_context={"nullable": None},
    )

    assert load_checkpoint(path)["flow_context"] == {"nullable": None}


async def test_new_base_never_replays_an_old_generation_journal(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-1", status="completed", response="one")
    stale_journal = writer.journal_path.read_bytes()
    await writer.flush()
    with writer.journal_path.open("ab") as stream:
        stream.write(stale_journal)

    recovered = load_checkpoint(path)
    assert recovered["ops"] == writer.ops
    assert recovered["_recovery"]["stale_generation_records"] == 1


async def test_cancellation_during_append_cannot_reuse_a_durable_sequence(
    tmp_path: Path, monkeypatch
):
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.flush()
    started = threading.Event()
    release = threading.Event()
    real_append = checkpoint_mod._append_journal_record

    def paused_append(journal_path: Path, record: dict) -> int:
        started.set()
        release.wait(timeout=2)
        return real_append(journal_path, record)

    monkeypatch.setattr(checkpoint_mod, "_append_journal_record", paused_append)
    first = asyncio.create_task(writer.record("agent-1", status="completed", response="one"))
    assert await asyncio.to_thread(started.wait, 1)
    first.cancel()
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await first

    await writer.record("agent-2", status="completed", response="two")

    recovered = load_checkpoint(path)
    assert set(recovered["ops"]) == {"agent-1", "agent-2"}
    assert "invalid_sequence" not in recovered.get("_recovery", {})
