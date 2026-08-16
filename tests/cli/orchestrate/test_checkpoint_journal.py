# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durability and write-amplification contracts for journaled flow checkpoints."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import threading
from pathlib import Path

import pytest

from lionagi.cli.orchestrate._checkpoint import (
    CHECKPOINT_VERSION,
    LEGACY_CHECKPOINT_VERSION,
    CheckpointWriter,
    JournalPathError,
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

    def slow_append(journal_path: Path, record: dict, *rest) -> int:
        started.set()
        release.wait(timeout=2)
        return real_append(journal_path, record, *rest)

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

    def paused_append(journal_path: Path, record: dict, *rest) -> int:
        started.set()
        release.wait(timeout=2)
        return real_append(journal_path, record, *rest)

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

    def _append_then_mutate(journal_path, record, *rest):
        written = original(journal_path, record, *rest)
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


# --- The journal is a named file, so its name is an attack surface -----------


async def test_a_symlinked_journal_is_refused_instead_of_followed(tmp_path: Path):
    """The journal is appended to and read back by name, never by descriptor
    held across the run. A symlink planted at that name sends every delta into
    whatever it points at, so a run's prompts and responses land in a file the
    run never chose."""
    path = tmp_path / "checkpoint.json"
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"")
    writer = _writer(path)
    await writer.flush()

    journal = writer.journal_path
    journal.unlink()
    journal.symlink_to(elsewhere)

    with pytest.raises(JournalPathError):
        await writer.record("agent-1", status="completed", response=1)
    # And nothing was appended to the target before the refusal.
    assert elsewhere.read_bytes() == b""


async def test_recovery_refuses_a_symlinked_journal_rather_than_reading_it(tmp_path: Path):
    """Recovery is the other half: contents arriving through a symlink were
    written by whoever planted it, and they are applied to state."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-1", status="completed", response=1)

    planted = tmp_path / "planted.journal"
    planted.write_bytes(writer.journal_path.read_bytes())
    writer.journal_path.unlink()
    writer.journal_path.symlink_to(planted)

    with pytest.raises(JournalPathError):
        load_checkpoint(path)


async def test_an_ordinary_journal_is_still_written_and_recovered(tmp_path: Path):
    """Control: the two refusals above pass if the journal stops working at all."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    await writer.record("agent-1", status="completed", response=1)

    assert not writer.journal_path.is_symlink()
    assert load_checkpoint(path)["ops"]["agent-1"]["response"] == 1


# --- Context deltas are decided by comparison, so the comparison is a contract


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (1, 1.0),
        ([1], [1.0]),
        (True, 1),
        ({"n": 1}, {"n": 1.0}),
    ],
)
async def test_a_json_visible_context_change_is_journaled(tmp_path: Path, before, after):
    """Python equality is coarser than JSON in the direction that loses data:
    1 == 1.0 and True == 1 both hold. A value that changed compares equal,
    journals nothing, and recovery restores the older one from the base."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    await writer.record("agent-1", status="completed", response=1, flow_context={"v": before})
    await writer.record("agent-2", status="completed", response=2, flow_context={"v": after})

    recovered = load_checkpoint(path)
    assert recovered["flow_context"]["v"] == after
    assert repr(recovered["flow_context"]["v"]) == repr(after)


async def test_an_unchanged_context_still_journals_no_delta(tmp_path: Path):
    """Control: the parametrized test above passes if every record journals a
    context delta regardless of whether anything changed, which would give back
    the write amplification the delta format exists to avoid."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    await writer.record("agent-1", status="completed", response=1, flow_context={"v": 1})
    await writer.record("agent-2", status="completed", response=2, flow_context={"v": 1})

    records = writer.journal_records()
    assert "flow_context" in records[0]
    assert "flow_context" not in records[1]


async def test_a_reordered_context_dict_is_not_reported_as_a_change(tmp_path: Path):
    """Same key set, different insertion order, is the same JSON document."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)

    await writer.record("a", status="completed", response=1, flow_context={"v": {"x": 1, "y": 2}})
    await writer.record("b", status="completed", response=2, flow_context={"v": {"y": 2, "x": 1}})

    assert "flow_context" not in writer.journal_records()[1]


# --- A record this build cannot read is not a record it may apply ------------


async def test_recovery_stops_at_an_unknown_record_version(tmp_path: Path):
    """An unreadable record applied anyway writes a guess into recovered state
    under the authority of a durable record."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-1", status="completed", response=1)

    state = json.loads(path.read_text())
    forged = {
        "version": CHECKPOINT_VERSION + 996,
        "generation": state["generation"],
        "seq": 2,
        "kind": "op",
        "entry": {"agent_id": "agent-2", "status": "completed", "response": 2},
    }
    with writer.journal_path.open("ab") as stream:
        stream.write(json.dumps(forged).encode() + b"\n")

    recovered = load_checkpoint(path)

    assert recovered["_recovery"]["unsupported_record_version"] is True
    assert recovered["_recovery"]["actual"] == CHECKPOINT_VERSION + 996
    # The record was reported, not applied, and the ones before it survive.
    assert "agent-2" not in recovered["ops"]
    assert "agent-1" in recovered["ops"]


# --- fsync decides durability, not whether the bytes are there ---------------


async def test_a_record_that_reached_the_file_does_not_reuse_its_sequence(
    tmp_path: Path, monkeypatch
):
    """A failing fsync leaves the record in the file: the write and the flush
    already happened, and fsync only says whether it survives a power loss. If
    the sequence number is treated as unspent, the next record carries the same
    seq, and recovery reads the duplicate as corruption, stops there, and throws
    away every completed record after it."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.flush()

    real_fsync = os.fsync
    armed = {"value": True}

    def flaky_fsync(descriptor):
        if armed["value"]:
            armed["value"] = False
            # Let the data land first, then fail. Failing before the real call
            # would be a different fault: bytes that never reached the file.
            real_fsync(descriptor)
            raise OSError(errno.EIO, "injected fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", flaky_fsync)

    with pytest.raises(OSError):
        await writer.record("agent-1", status="completed", response=1)
    assert armed["value"] is False, "the injected failure never fired"

    await writer.record("agent-2", status="completed", response=2)

    assert [record["seq"] for record in writer.journal_records()] == [1, 2]
    recovered = load_checkpoint(path)
    assert "_recovery" not in recovered, recovered.get("_recovery")
    assert set(recovered["ops"]) == {"agent-1", "agent-2"}


async def test_a_record_that_never_reached_the_file_leaves_its_sequence_unspent(
    tmp_path: Path, monkeypatch
):
    """The other side of the same decision. A write that failed before anything
    was appended must not consume a number, or the journal gains a gap and
    recovery stops at it for the mirror-image reason."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.flush()

    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    def refuse_open(*args, **kwargs):
        raise OSError(errno.EACCES, "injected open failure")

    monkeypatch.setattr(checkpoint_mod, "_open_no_follow", refuse_open)
    with pytest.raises(OSError):
        await writer.record("agent-1", status="completed", response=1)
    monkeypatch.undo()

    await writer.record("agent-2", status="completed", response=2)

    assert [record["seq"] for record in writer.journal_records()] == [1]
    recovered = load_checkpoint(path)
    assert "_recovery" not in recovered, recovered.get("_recovery")
    assert set(recovered["ops"]) == {"agent-2"}


# --- what the files hold is nobody else's business ---------------------------


async def test_checkpoint_and_journal_are_created_owner_only(tmp_path: Path):
    """Both files carry raw operation responses and the shared flow context.

    Creating them through an ordinary open takes the mode from the process
    umask, which is 022 by default and publishes a world-readable file.
    """
    previous_umask = os.umask(0o022)
    try:
        path = tmp_path / "checkpoint.json"
        writer = _writer(path, compact_every=1)
        await writer.record("agent-1", status="completed", response={"secret": "value"})

        assert path.exists() and writer.journal_path.exists()
        for created in (path, writer.journal_path):
            mode = created.stat().st_mode & 0o777
            assert mode & 0o077 == 0, f"{created.name} is readable by others: {mode:o}"
    finally:
        os.umask(previous_umask)


async def test_the_legacy_checkpoint_is_created_owner_only_too(tmp_path: Path):
    """The unjournaled path writes the same responses to the same kind of file."""
    previous_umask = os.umask(0o022)
    try:
        path = tmp_path / "checkpoint.json"
        writer = CheckpointWriter(
            path=path,
            session_id="session-1",
            prompt="run the plan",
            plan=[],
            config={},
            version=LEGACY_CHECKPOINT_VERSION,
        )
        await writer.record("agent-1", status="completed", response={"secret": "value"})

        assert path.stat().st_mode & 0o077 == 0
    finally:
        os.umask(previous_umask)


async def test_a_symlinked_journal_is_refused_where_the_open_flag_does_not_exist(
    tmp_path: Path, monkeypatch
):
    """O_NOFOLLOW is absent on some platforms this ships to, and the refusal
    cannot be absent with it: a symlink at the journal name redirects every
    delta into a file this process never chose and feeds recovery records it
    never wrote."""
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    monkeypatch.setattr(checkpoint_mod, "_NOFOLLOW", 0)

    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.flush()

    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("")
    writer.journal_path.unlink()
    writer.journal_path.symlink_to(elsewhere)

    with pytest.raises(checkpoint_mod.JournalPathError):
        await writer.record("agent-1", status="completed", response=1)
    assert elsewhere.read_text() == "", "the write followed the symlink"


# --- the baseline has to be the writer's own copy ----------------------------


async def test_a_resumed_writer_does_not_share_nested_context_with_the_executor(
    tmp_path: Path,
):
    """Resume hands the same dict to the writer and to the executor, which goes
    on mutating it in place. A baseline sharing those nested values moves with
    every mutation, so the comparison finds nothing to journal and the change is
    gone at the next recovery."""
    path = tmp_path / "checkpoint.json"
    live: dict[str, object] = {"findings": ["first"]}
    writer = CheckpointWriter(
        path=path,
        session_id="session-1",
        prompt="run the plan",
        plan=[],
        config={},
        version=CHECKPOINT_VERSION,
        flow_context=dict(live),
    )
    await writer.flush()

    live["findings"].append("second")  # type: ignore[attr-defined]
    await writer.record("agent-1", status="completed", response=1, flow_context=live)

    recovered = load_checkpoint(path)
    assert "_recovery" not in recovered, recovered.get("_recovery")
    assert recovered["flow_context"]["findings"] == ["first", "second"]


# --- a record on disk that memory forgot is a record compaction deletes ------


async def test_an_fsync_failed_record_survives_the_next_compaction(tmp_path: Path, monkeypatch):
    """Compaction rewrites the base from memory and then clears the journal. An
    operation whose record reached the file but was never applied in memory is
    in neither afterwards, so a durable completion is lost for good."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path, compact_every=2)
    await writer.flush()

    real_fsync = os.fsync
    armed = {"value": True}

    def flaky_fsync(descriptor):
        if armed["value"]:
            armed["value"] = False
            real_fsync(descriptor)
            raise OSError(errno.EIO, "injected fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", flaky_fsync)

    with pytest.raises(OSError):
        await writer.record("agent-1", status="completed", response=1)
    assert armed["value"] is False, "the injected failure never fired"

    # Reaches compact_every and rebases from memory, discarding the journal.
    await writer.record("agent-2", status="completed", response=2)
    assert writer.journal_records() == []

    recovered = load_checkpoint(path)
    assert set(recovered["ops"]) == {"agent-1", "agent-2"}
    assert recovered["ops"]["agent-1"]["response"] == 1


# --- a stopped write must not swallow the record that follows it -------------


async def test_a_partial_append_does_not_glue_itself_to_the_next_record(
    tmp_path: Path, monkeypatch
):
    """A write that stops halfway leaves bytes with no newline. Reporting that
    as a write that never happened leaves the sequence unspent, so the retry
    lands directly behind the fragment and the two become one unreadable line --
    which recovery stops at, discarding every valid completion after it."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)

    real_write = os.write
    state = {"phase": "short"}

    def truncating_write(descriptor, payload):
        # A short write is not an error: it returns a smaller count and the
        # caller is expected to send the rest. The failure lands on that retry,
        # which is what leaves a fragment in the file.
        if state["phase"] == "short" and len(payload) > 8:
            state["phase"] = "fail"
            return real_write(descriptor, payload[:8])
        if state["phase"] == "fail":
            state["phase"] = "done"
            raise OSError(errno.EIO, "injected write failure")
        return real_write(descriptor, payload)

    monkeypatch.setattr(os, "write", truncating_write)

    with pytest.raises(OSError):
        await writer.record("agent-1", status="completed", response=1)
    assert state["phase"] == "done", "the injected failure never fired"

    await writer.record("agent-2", status="completed", response=2)
    monkeypatch.undo()

    lines = writer.journal_path.read_bytes().splitlines()
    # Three lines: the first record, the fragment on its own, and the next
    # record intact rather than appended to the fragment.
    assert len(lines) == 3, lines
    assert json.loads(lines[0])["entry"]["agent_id"] == "agent-0"
    assert json.loads(lines[2])["entry"]["agent_id"] == "agent-2"

    recovered = load_checkpoint(path)
    assert recovered["_recovery"]["invalid_record"] is True
    assert set(recovered["ops"]) == {"agent-0"}


# --- an id with no outcome is not a lighter record ---------------------------


async def test_recovery_refuses_a_record_that_carries_an_id_but_no_outcome(tmp_path: Path):
    """Applied, such a record makes resume skip an operation whose result it
    does not have, and report success doing it."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-1", status="completed", response=1)

    state = json.loads(path.read_text())
    with writer.journal_path.open("ab") as journal:
        journal.write(
            json.dumps(
                {
                    "version": CHECKPOINT_VERSION,
                    "generation": state["generation"],
                    "seq": 2,
                    "kind": "op",
                    "entry": {"agent_id": "agent-2"},
                }
            ).encode()
            + b"\n"
        )

    recovered = load_checkpoint(path)
    assert recovered["_recovery"]["invalid_record"] is True
    assert set(recovered["ops"]) == {"agent-1"}


# --- what is already sitting at the journal name -----------------------------


async def test_the_journal_refuses_an_inode_that_is_linked_from_somewhere_else(tmp_path: Path):
    """0600 describes a file this process created. A hard link planted at the
    journal name was created by somebody else, at whatever mode they chose, and
    every record appended to it lands in their file too -- with the run's raw
    operation responses and its whole flow context in it."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)

    readable = tmp_path / "somebody-elses-file.jsonl"
    readable.write_bytes(b"")
    # Owner-only, like a journal we made ourselves. The link is the only thing
    # wrong with it, so nothing but the link check can be what refuses it --
    # a 0644 fixture here passes against a writer that has no link check at all.
    readable.chmod(0o600)
    writer.journal_path.unlink()
    os.link(readable, writer.journal_path)
    assert writer.journal_path.stat().st_nlink == 2, "the fixture did not create a link"
    assert writer.journal_path.stat().st_mode & 0o077 == 0, "the fixture is not owner-only"

    with pytest.raises(JournalPathError):
        await writer.record("agent-1", status="completed", response={"token": "sk-secret"})

    assert b"sk-secret" not in readable.read_bytes()


async def test_the_journal_refuses_a_file_readable_beyond_its_owner(tmp_path: Path):
    """The same hole without the link: a file already at the journal name keeps
    the permissions it was created with, because the creation mode applies to
    a creation and nothing was created."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)

    writer.journal_path.chmod(0o644)

    with pytest.raises(JournalPathError):
        await writer.record("agent-1", status="completed", response={"token": "sk-secret"})

    assert b"sk-secret" not in writer.journal_path.read_bytes()


async def test_a_private_journal_of_our_own_is_still_accepted(tmp_path: Path):
    """Control for the two refusals above. Both of them pass trivially against
    a writer that refuses every journal, so the ordinary case has to be shown
    working under the same checks."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)
    await writer.record("agent-1", status="completed", response={"token": "sk-secret"})

    assert writer.journal_path.stat().st_nlink == 1
    assert writer.journal_path.stat().st_mode & 0o077 == 0
    assert b"sk-secret" in writer.journal_path.read_bytes()
    assert set(load_checkpoint(path)["ops"]) == {"agent-0", "agent-1"}


def _fifo_at(path: Path) -> None:
    """Replace `path` with an owner-only FIFO.

    Owner-only on purpose. A FIFO created at the default umask is 0644, which
    the permission check refuses on its own -- and a test that trips that check
    says nothing about whether the file type is examined at all.
    """
    path.unlink()
    os.mkfifo(path, 0o600)
    path.chmod(0o600)
    assert path.stat().st_mode & 0o077 == 0, "the fixture is not owner-only"


async def test_the_journal_refuses_a_fifo_even_while_one_is_being_read(tmp_path: Path):
    """A FIFO passes a symlink check, because it is not a symlink. With a
    reader already attached the write-side open succeeds outright, so nothing
    about the open refuses it: what the writer does with the descriptor after
    opening it is the only thing that can."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)

    _fifo_at(writer.journal_path)
    # A non-blocking read-side open returns straight away and leaves a reader
    # attached, so the writer's open cannot fail for want of one.
    reader = os.open(writer.journal_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(JournalPathError):
            await writer.record("agent-1", status="completed", response=1)
    finally:
        os.close(reader)


async def test_the_journal_does_not_wait_on_a_fifo_nobody_is_reading(tmp_path: Path):
    """The same FIFO with no reader. A write-side open of one waits for a
    reader that is never coming, and the checkpoint write holds the writer's
    lock, so what stalls is the run.

    The timeout bounds the call rather than the assertion. Without the
    non-blocking open this does not raise late, it does not return, so a
    regression here reports as a hang and not as a pass.
    """
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)

    _fifo_at(writer.journal_path)

    with pytest.raises(JournalPathError):
        await asyncio.wait_for(writer.record("agent-1", status="completed", response=1), timeout=10)


async def test_recovery_refuses_a_fifo_at_the_journal_name(tmp_path: Path):
    """Recovery opens the same name to read it, and gets whatever is there."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)

    _fifo_at(writer.journal_path)

    with pytest.raises(JournalPathError):
        await asyncio.wait_for(asyncio.to_thread(load_checkpoint, path), timeout=10)


# --- a close that fails after the bytes are already down ---------------------


async def test_a_failing_close_does_not_hand_back_the_sequence_it_already_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """close() reports errors the write never surfaced, and it runs after the
    record is in the file and fsynced. Raised from a `finally`, it replaced the
    byte count with itself -- so the writer read a landed record as one that
    never happened, kept the sequence number, and spent it again on the next
    record. Recovery meets the same sequence twice, stops there, and discards
    every completion after it."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-0", status="completed", response=0)

    real_close = os.close
    state = {"fired": False}

    def failing_close(descriptor: int) -> None:
        if not state["fired"] and os.fstat(descriptor).st_size > 0:
            state["fired"] = True
            real_close(descriptor)
            raise OSError(errno.EIO, "injected close failure")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", failing_close)
    with contextlib.suppress(Exception):
        await writer.record("agent-1", status="completed", response=1)
    monkeypatch.undo()
    assert state["fired"], "the injected failure never fired"

    await writer.record("agent-2", status="completed", response=2)

    seqs = [json.loads(line)["seq"] for line in writer.journal_path.read_bytes().splitlines()]
    assert len(seqs) == len(set(seqs)), f"a sequence number was spent twice: {seqs}"

    recovered = load_checkpoint(path)
    assert "invalid_sequence" not in recovered.get("_recovery", {}), recovered.get("_recovery")
    assert "agent-2" in recovered["ops"], recovered["ops"]


async def test_the_journal_refuses_a_private_file_that_replaced_the_one_it_created(tmp_path: Path):
    """A substitute made by a process running as us looks exactly like a healthy
    journal: regular, one link, our uid, 0600. Only its identity differs, and
    records carry raw responses and the shared flow context."""
    from lionagi.cli.orchestrate import _checkpoint as checkpoint_mod

    path = tmp_path / "checkpoint.json"
    writer = _writer(path)
    await writer.record("agent-1", status="completed", response="one")

    journal = writer.journal_path
    created_inode = journal.stat().st_ino
    substitute = tmp_path / "substitute"
    substitute.write_bytes(b"")
    substitute.chmod(0o600)
    os.replace(substitute, journal)
    assert journal.stat().st_ino != created_inode

    # The control that makes this test about identity rather than about the
    # kind-and-ownership guard: that guard accepts the substitute.
    descriptor = os.open(journal, os.O_RDONLY)
    try:
        checkpoint_mod._refuse_unsafe_inode(descriptor, journal)
    finally:
        os.close(descriptor)

    with pytest.raises(JournalPathError):
        await writer.record("agent-2", status="completed", response="two")


async def test_a_journal_this_writer_created_keeps_taking_records(tmp_path: Path):
    """The control for the refusal above, including across a compaction, which
    replaces the journal on purpose and has to leave it usable."""
    path = tmp_path / "checkpoint.json"
    writer = _writer(path, compact_every=2)

    for i in range(6):
        await writer.record(f"agent-{i}", status="completed", response=i)

    recovered = load_checkpoint(path)
    assert list(recovered["ops"]) == [f"agent-{i}" for i in range(6)]


async def test_a_journal_reset_deferred_by_a_failure_is_pinned_when_it_finally_happens(
    tmp_path: Path, monkeypatch
):
    """A base write marks the journal for reset before performing it, so a
    failure there leaves the reset owed to the next record. That reset replaces
    the journal exactly as the base write would have, and the append after it
    is measured against whatever the last reset produced."""
    from lionagi.cli.orchestrate import _checkpoint as checkpoint_mod

    path = tmp_path / "checkpoint.json"
    writer = _writer(path, compact_every=1)
    real = checkpoint_mod._atomic_write_bytes
    resets: list[int] = []

    def fail_the_compaction_reset(target: Path, payload: bytes) -> int:
        if target == writer.journal_path:
            resets.append(1)
            if len(resets) == 2:
                raise OSError(errno.EIO, "journal reset failed")
        return real(target, payload)

    monkeypatch.setattr(checkpoint_mod, "_atomic_write_bytes", fail_the_compaction_reset)

    with pytest.raises(OSError):
        await writer.record("agent-1", status="completed", response="one")
    assert len(resets) == 2

    await writer.record("agent-2", status="completed", response="two")

    recovered = load_checkpoint(path)
    assert list(recovered["ops"]) == ["agent-1", "agent-2"]
