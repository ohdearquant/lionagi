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


async def test_journal_records_in_place_nested_context_updates(tmp_path: Path):
    """The caller mutates the shared context workspace in place and passes the
    same object every time. A snapshot that shared nested values with it would
    compare equal, journal nothing, and resume with context the run had already
    moved past.
    """
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    context: dict = {"shared": {"first": 1}, "top": "a"}
    await writer.record("agent-1", status="completed", response="one", flow_context=context)

    # In place, on the object the caller still holds — no rebinding.
    context["shared"]["second"] = 2
    await writer.record("agent-2", status="completed", response="two", flow_context=context)

    # No compaction has run (compact_every=128), so recovery is journal replay:
    # exactly the crash window the journal exists to cover.
    assert writer.journal_path.read_bytes() != b""
    recovered = load_checkpoint(path)
    assert recovered["flow_context"] == {"shared": {"first": 1, "second": 2}, "top": "a"}


async def test_journal_records_in_place_nested_context_updates_for_spawned_nodes(tmp_path: Path):
    """Same defect, same fix, on the reactively-spawned path."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    context: dict = {"shared": {"first": 1}}
    await writer.record_spawned(
        "node-1", status="completed", response="one", flow_context=context, operation="communicate"
    )

    context["shared"]["second"] = 2
    await writer.record_spawned(
        "node-2", status="completed", response="two", flow_context=context, operation="communicate"
    )

    recovered = load_checkpoint(path)
    assert recovered["flow_context"] == {"shared": {"first": 1, "second": 2}}


def _inject_after_the_journal_write(monkeypatch, context: dict, mutate) -> list:
    """Mutate the caller's live workspace after the delta is on disk.

    The journal append runs on a worker thread, and ``record`` is still
    awaiting it, so mutating once the record has been serialized lands the
    change in the window between what the journal saw and what the baseline
    will be.

    Mutating *before* the append instead proves nothing, and it is worth
    saying why: the delta's ``set`` values are references into the caller's
    live nested containers, so a change made before serialization is written
    out with the record and no loss occurs. The window this closes is the one
    after those bytes exist.

    Returns a list that is non-empty once the injection has run, so a test can
    assert its own probe fired rather than passing because nothing happened.
    """
    from lionagi.cli.orchestrate import _checkpoint as checkpoint_mod

    original = checkpoint_mod._append_journal_record
    fired: list[bool] = []

    def _append_then_mutate(journal_path, record):
        written = original(journal_path, record)
        if not fired:
            fired.append(True)
            mutate(context)
        return written

    monkeypatch.setattr(checkpoint_mod, "_append_journal_record", _append_then_mutate)
    return fired


async def test_a_mutation_during_the_write_cannot_enter_the_baseline_unjournaled(
    tmp_path: Path, monkeypatch
):
    """A value the caller adds while the write is in flight still reaches recovery.

    Deriving the delta from the live context and then snapshotting the live
    context again reads it twice, on opposite sides of an await. A mutation
    landing between the two enters the baseline without entering any delta,
    and the baseline is what the next comparison uses, so the value never gets
    journaled at all and recovery silently drops it.
    """
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    context: dict = {"shared": {"first": 1}}

    fired = _inject_after_the_journal_write(
        monkeypatch, context, lambda c: c["shared"].__setitem__("injected", "during-the-write")
    )
    await writer.record("agent-1", status="completed", response="one", flow_context=context)
    assert fired, "the injection never ran, so this test proves nothing"

    # A later completion is the only remaining chance to journal that value.
    await writer.record("agent-2", status="completed", response="two", flow_context=context)

    recovered = load_checkpoint(path)
    assert recovered["flow_context"] == {"shared": {"first": 1, "injected": "during-the-write"}}


async def test_a_mutation_during_a_spawned_write_cannot_enter_the_baseline_unjournaled(
    tmp_path: Path, monkeypatch
):
    """Same window, same loss, on the reactively-spawned path."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    context: dict = {"shared": {"first": 1}}

    fired = _inject_after_the_journal_write(
        monkeypatch, context, lambda c: c["shared"].__setitem__("injected", "during-the-write")
    )
    await writer.record_spawned(
        "node-1", status="completed", response="one", flow_context=context, operation="communicate"
    )
    assert fired, "the injection never ran, so this test proves nothing"

    await writer.record_spawned(
        "node-2", status="completed", response="two", flow_context=context, operation="communicate"
    )

    recovered = load_checkpoint(path)
    assert recovered["flow_context"] == {"shared": {"first": 1, "injected": "during-the-write"}}


async def test_context_snapshot_falls_back_when_a_value_refuses_deep_copy(tmp_path: Path):
    """A value that cannot be deep-copied must not turn journaling into a crash;
    it still has to leave a baseline that is not aliased to the caller's object.
    """
    from lionagi.cli.orchestrate._checkpoint import _context_snapshot

    class _NoCopy:
        def __deepcopy__(self, memo):
            raise TypeError("cannot deep-copy this")

        def __str__(self) -> str:
            return "opaque"

    source = {"nested": {"keep": 1}, "opaque": _NoCopy()}
    snapshot = _context_snapshot(source)

    assert snapshot["nested"] == {"keep": 1}
    assert snapshot["nested"] is not source["nested"]
    assert snapshot["opaque"] == "opaque"
