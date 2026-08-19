# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for MessagePersistRetryQueue: permanent-failure classification
must not head-of-line-block later messages."""

from __future__ import annotations

import logging
from typing import Any

from lionagi.hooks._message_retry import MessagePersistRetryQueue, PendingMessageEvent


class _FakeDB:
    """Stands in for StateDB._persist_live_message: raises ValueError for ids in
    `fail_ids` (mirrors _validate_message's deterministic validation failure) and
    records everything else as persisted."""

    def __init__(self, fail_ids: set[str]) -> None:
        self.fail_ids = fail_ids
        self.persisted: list[str] = []

    async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
        if message["id"] in self.fail_ids:
            raise ValueError("messages.content is NOT NULL")
        self.persisted.append(message["id"])


def _event(msg_id: str) -> PendingMessageEvent:
    return PendingMessageEvent(
        message={"id": msg_id, "content": "x", "role": "user"},
        session_id="s1",
    )


async def test_permanent_validation_error_does_not_block_later_messages():
    """A message at the queue head that permanently fails validation must be
    dropped, not left blocking every message submitted after it."""
    db = _FakeDB(fail_ids={"bad-1"})
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    await queue.submit(_event("bad-1"))
    await queue.submit(_event("good-1"))

    assert db.persisted == ["good-1"], (
        "good-1 must be persisted even though bad-1 (queued ahead of it) "
        "permanently fails validation"
    )
    assert queue.pending_count == 0


async def test_permanent_validation_error_is_dropped_not_persisted():
    """The permanently-invalid message itself is never persisted (dropped, not retried)."""
    db = _FakeDB(fail_ids={"bad-1"})
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    ok = await queue.submit(_event("bad-1"))

    assert ok is True  # queue fully drained (the bad item was dropped, not stuck)
    assert db.persisted == []
    assert queue.pending_count == 0


async def test_permanent_validation_error_logs_warning(caplog):
    db = _FakeDB(fail_ids={"bad-1"})
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    with caplog.at_level(logging.WARNING, logger="test"):
        await queue.submit(_event("bad-1"))

    assert any(
        "dropping malformed message" in rec.message and "non-retryable" in rec.message
        for rec in caplog.records
    )


async def test_transient_error_still_head_of_line_blocks_until_deferred():
    """Non-ValueError (transient) failures keep the pre-existing ordered-retry
    behavior: the head item stays queued and blocks later items until it
    either succeeds or the queue defers after MAX_CONSECUTIVE_FAILURES."""

    class _AlwaysBusyDB:
        def __init__(self) -> None:
            self.persisted: list[str] = []

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            raise RuntimeError("simulated sqlite busy")

    db = _AlwaysBusyDB()
    queue = MessagePersistRetryQueue(db, logger=logging.getLogger("test"), owner="b1")

    await queue.submit(_event("stuck-1"))
    await queue.submit(_event("later-1"))

    assert db.persisted == []
    assert queue.pending_count == 2


class _RefusingDB:
    """Refuses every write, the way a contended sqlite does."""

    def __init__(self) -> None:
        self.persisted: list[str] = []
        self.attempts = 0

    async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
        self.attempts += 1
        raise RuntimeError("database is locked")


async def _deferred_queue(db, logger):
    """A queue driven past its consecutive-failure limit, as real traffic does."""
    queue = MessagePersistRetryQueue(db, logger=logger, owner="b1")
    for i in range(4):
        await queue.submit(_event(f"m{i}"))
    return queue


async def test_a_teardown_that_loses_messages_says_so(caplog):
    """The last attempt these events get is the one that used to say nothing.

    By teardown the queue is already deferred, so the state log stays quiet
    because nothing changed, and the only other signal is a return value.
    """
    db = _RefusingDB()
    logger = logging.getLogger("test")
    queue = await _deferred_queue(db, logger)
    assert queue.pending_count == 4

    with caplog.at_level(logging.ERROR, logger="test"):
        flushed = await queue.flush_final()

    assert flushed is False
    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 1, "the loss is reported once, not per event and not never"
    assert "lost" in losses[0].message
    # The count is the part nobody can reconstruct once the events are gone.
    assert "4" in losses[0].message


async def test_a_healthy_teardown_stays_quiet(caplog):
    """The other direction. A report that always fires is a report nobody reads,
    and this one exists to be believed the once it appears."""
    db = _FakeDB(fail_ids=set())
    logger = logging.getLogger("test")
    queue = MessagePersistRetryQueue(db, logger=logger, owner="b1")
    await queue.submit(_event("good-1"))

    with caplog.at_level(logging.DEBUG, logger="test"):
        flushed = await queue.flush_final()

    assert flushed is True
    assert db.persisted == ["good-1"]
    assert [rec for rec in caplog.records if rec.levelno >= logging.WARNING] == []


async def test_a_teardown_that_recovers_stays_quiet(caplog):
    """A queue that was failing and then drains is not a loss. Reporting one
    here is exactly the crying-wolf this is meant to avoid."""

    class _RecoveringDB:
        def __init__(self) -> None:
            self.persisted: list[str] = []
            self.refuse = True

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            if self.refuse:
                raise RuntimeError("database is locked")
            self.persisted.append(message["id"])

    db = _RecoveringDB()
    logger = logging.getLogger("test")
    queue = await _deferred_queue(db, logger)
    db.refuse = False

    with caplog.at_level(logging.WARNING, logger="test"):
        flushed = await queue.flush_final()

    assert flushed is True
    assert queue.pending_count == 0
    assert db.persisted == ["m0", "m1", "m2", "m3"], "order is preserved across the recovery"
    assert [rec for rec in caplog.records if rec.levelno >= logging.ERROR] == []


async def test_the_bus_reports_whether_its_queues_emptied(caplog):
    """The bus teardown discarded this, so nothing downstream could tell."""
    from lionagi.hooks.bus import HookBus

    logger = logging.getLogger("test")
    bus = HookBus()
    bus._message_retry_queues = {"b1": await _deferred_queue(_RefusingDB(), logger)}

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await bus.flush_message_retries() is False

    healthy = MessagePersistRetryQueue(_FakeDB(fail_ids=set()), logger=logger, owner="b2")
    await healthy.submit(_event("good-1"))
    bus._message_retry_queues = {"b2": healthy}

    assert await bus.flush_message_retries() is True


async def test_the_run_teardown_reports_whether_its_queues_emptied():
    """The second teardown that discarded it. What runs after this reads the
    run's completion evidence."""
    from lionagi.cli._runs import _flush_pending_message_events

    logger = logging.getLogger("test")
    lost = await _deferred_queue(_RefusingDB(), logger)
    assert await _flush_pending_message_events({"message_retry_queues": [lost]}) == {
        "lost": 4,
        "queues": [{"owner": "b1", "lost": 4}],
    }

    healthy = MessagePersistRetryQueue(_FakeDB(fail_ids=set()), logger=logger, owner="b2")
    await healthy.submit(_event("good-1"))
    assert await _flush_pending_message_events({"message_retry_queues": [healthy]}) is None


async def test_both_teardown_paths_over_one_queue_report_the_loss_once(caplog):
    """The two teardowns reach the same queue, and neither can see the other.

    The hook bus flushes on ``SESSION_END`` and the run teardown flushes before
    reading completion evidence. One queue traversed by both would report its
    loss twice, and one loss restated reads as two incidents — the crying-wolf
    the report exists to avoid.

    Asserted through both real entry points rather than by calling
    ``flush_final`` twice, because the duplication is a property of the two
    paths meeting, not of the method.
    """
    from lionagi.cli._runs import _flush_pending_message_events
    from lionagi.hooks.bus import HookBus

    logger = logging.getLogger("test")
    queue = await _deferred_queue(_RefusingDB(), logger)

    bus = HookBus()
    bus._message_retry_queues = {"b1": queue}

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await bus.flush_message_retries() is False
        assert await _flush_pending_message_events({"message_retry_queues": [queue]}) is not None

    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 1, (
        f"one loss, reported once across both teardown paths; got {len(losses)}"
    )
    assert "4" in losses[0].message

    # Both calls still returned False. Suppressing the repeated report must not
    # suppress the answer a caller acts on.
    assert queue.pending_count == 4


async def test_a_loss_that_grows_between_teardowns_is_reported_again(caplog):
    """The over-suppression arm. A second call is silenced because it restates
    one fact, not because a queue may only ever report once: events lost since
    the first report are a different fact and nobody else will name them."""
    logger = logging.getLogger("test")
    db = _RefusingDB()
    queue = await _deferred_queue(db, logger)

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await queue.flush_final() is False
        await queue.submit(_event("m4"))
        assert await queue.flush_final() is False

    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 2, "a changed count is a new fact"
    assert "4" in losses[0].message
    assert "5" in losses[1].message


async def test_a_recovery_between_teardowns_does_not_swallow_a_later_loss(caplog):
    """The marker is cleared by a flush that succeeds, so a queue that recovers
    and then loses new events reports that loss rather than reading it as a
    repeat of the one already on the record."""

    class _FlakyDB:
        def __init__(self) -> None:
            self.persisted: list[str] = []
            self.refuse = True

        async def _persist_live_message(self, message: dict[str, Any], **kwargs: Any) -> None:
            if self.refuse:
                raise RuntimeError("database is locked")
            self.persisted.append(message["id"])

    logger = logging.getLogger("test")
    db = _FlakyDB()
    queue = await _deferred_queue(db, logger)

    with caplog.at_level(logging.ERROR, logger="test"):
        assert await queue.flush_final() is False  # 4 lost, reported
        db.refuse = False
        assert await queue.flush_final() is True  # drains, marker cleared
        db.refuse = True
        for i in range(4):
            await queue.submit(_event(f"later-{i}"))
        assert await queue.flush_final() is False  # 4 again, but a new 4

    losses = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(losses) == 2, (
        "the second loss has the same count as the first and is still a different loss"
    )


# The loss reaching the run's terminal record. Until this, the return value
# above was read by nothing: both teardowns awaited it and dropped it, so a run
# that lost messages closed as "run.completed.ok / Run completed successfully."


async def _closed_out(tmp_path, sid: str, queues: list, **kwargs) -> dict:
    """Take one session through the real teardown and read back what it wrote.

    ``teardown_persist`` closes the handle it was given, so the read-back opens
    its own -- the row is what a later reader sees, which is the whole subject
    here.
    """
    import json

    from sqlalchemy import text

    from lionagi.cli._runs import teardown_persist
    from lionagi.state.db import StateDB

    path = tmp_path / f"{sid}.db"
    db = StateDB(path)
    await db.open()
    await db.create_progression(f"prog-{sid}")
    await db.create_session(
        {
            "id": sid,
            "progression_id": f"prog-{sid}",
            "status": "running",
            "started_at": 1_700_000_000.0,
        }
    )
    final = await teardown_persist(
        {
            "db": db,
            "session_id": sid,
            "session_prog_id": f"prog-{sid}",
            "message_retry_queues": queues,
        },
        **kwargs,
    )

    reader = StateDB(path)
    await reader.open()
    try:
        row = await reader.get_session(sid)
        async with reader._read() as conn:
            result = await conn.execute(
                text(
                    "SELECT metadata FROM status_transitions "
                    "WHERE entity_id = :sid ORDER BY created_at DESC LIMIT 1"
                ),
                {"sid": sid},
            )
            recorded = result.scalar()
    finally:
        await reader.close()
    if isinstance(recorded, str):
        recorded = json.loads(recorded)
    return {"final": final, "row": row, "transition_metadata": recorded or {}}


async def _healthy_queue(owner: str = "b2"):
    queue = MessagePersistRetryQueue(
        _FakeDB(fail_ids=set()), logger=logging.getLogger("test"), owner=owner
    )
    await queue.submit(_event("good-1"))
    return queue


async def test_a_run_that_lost_messages_does_not_close_as_a_clean_success(tmp_path):
    """What the reported incident looked like from the row: events gone, and the
    session saying the run completed successfully."""
    queue = await _deferred_queue(_RefusingDB(), logging.getLogger("test"))

    out = await _closed_out(tmp_path, "sess-loss", [queue], status="completed")

    assert out["final"] == "completed", "the run's own work stands; this is not a failure"
    assert out["row"]["status_reason_code"] == "run.completed.message_loss"
    assert "4 live message event(s) were never written" in out["row"]["status_reason_summary"]


async def test_the_terminal_row_names_which_queue_lost_how_many(tmp_path):
    """A count with no owner cannot be chased. Two queues, only one losing."""
    lost = await _deferred_queue(_RefusingDB(), logging.getLogger("test"))

    out = await _closed_out(
        tmp_path, "sess-loss-refs", [await _healthy_queue(), lost], status="completed"
    )

    assert out["row"]["status_evidence_refs"] == [
        {"kind": "message_persist_loss", "id": "b1", "label": "4 event(s) lost"}
    ], "the queue that emptied contributes nothing to name"


async def test_a_run_that_lost_nothing_is_untouched(tmp_path):
    """The control. A clean run must still read clean, or the annotation above
    is noise on every row rather than a signal on the affected ones."""
    out = await _closed_out(tmp_path, "sess-clean", [await _healthy_queue()], status="completed")

    assert out["row"]["status_reason_code"] == "run.completed.ok"
    assert out["row"]["status_evidence_refs"] in (None, [])
    assert "message_persist_loss" not in out["transition_metadata"]


async def test_a_failed_run_keeps_its_own_failure(tmp_path):
    """The loss annotates; it never overwrites why a run actually failed."""
    queue = await _deferred_queue(_RefusingDB(), logging.getLogger("test"))

    out = await _closed_out(
        tmp_path,
        "sess-failed",
        [queue],
        status="failed",
        exception=RuntimeError("the real cause"),
    )

    assert out["final"] == "failed"
    assert out["row"]["status_reason_code"] == "run.failed.exception"
    assert out["transition_metadata"]["message_persist_loss"]["lost"] == 4, (
        "annotated, so the loss is still findable on a run that failed for its own reasons"
    )


async def test_the_structured_loss_is_recorded_for_a_reader_that_wants_the_shape(tmp_path):
    """reason_summary is prose. The audit trail carries the numbers."""
    queue = await _deferred_queue(_RefusingDB(), logging.getLogger("test"))

    out = await _closed_out(tmp_path, "sess-loss-meta", [queue], status="completed")

    assert out["transition_metadata"]["message_persist_loss"] == {
        "lost": 4,
        "queues": [{"owner": "b1", "lost": 4}],
    }


# The deferred leg. A timed-out leg defers its terminal write to the leg that resumes
# the session, and its queue is unrouted right after. It is the only thing that ever
# knew about its own loss, so if it does not leave it behind the resumed leg's clean
# terminal write is the only record and the loss is gone.


async def _teardown_on(path, sid: str, queues: list, **kwargs):
    from lionagi.cli._runs import teardown_persist
    from lionagi.state.db import StateDB

    db = StateDB(path)
    await db.open()
    existing = await db.get_session(sid)
    if existing is None:
        await db.create_progression(f"prog-{sid}")
        await db.create_session(
            {
                "id": sid,
                "progression_id": f"prog-{sid}",
                "status": "running",
                "started_at": 1_700_000_000.0,
            }
        )
    return await teardown_persist(
        {
            "db": db,
            "session_id": sid,
            "session_prog_id": f"prog-{sid}",
            "message_retry_queues": queues,
        },
        **kwargs,
    )


async def _session_row(path, sid: str) -> dict:
    from lionagi.state.db import StateDB

    reader = StateDB(path)
    await reader.open()
    try:
        return await reader.get_session(sid)
    finally:
        await reader.close()


async def test_a_deferred_legs_loss_reaches_the_terminal_write_that_resumes_it(tmp_path):
    path = tmp_path / "deferred.db"
    sid = "sess-deferred"
    logger = logging.getLogger("test")

    await _teardown_on(
        path,
        sid,
        [await _deferred_queue(_RefusingDB(), logger)],
        status="timed_out",
        defer_terminal=True,
    )
    row = await _session_row(path, sid)
    assert row["status"] == "running", "the deferred leg writes no terminal status"

    await _teardown_on(path, sid, [], status="completed")

    row = await _session_row(path, sid)
    assert row["status_reason_code"] == "run.completed.message_loss"
    assert "4 live message event(s) were never written" in row["status_reason_summary"]


async def test_both_legs_losses_are_added_up_rather_than_one_replacing_the_other(tmp_path):
    path = tmp_path / "two-legs.db"
    sid = "sess-two-legs"
    logger = logging.getLogger("test")

    await _teardown_on(
        path,
        sid,
        [await _deferred_queue(_RefusingDB(), logger)],
        status="timed_out",
        defer_terminal=True,
    )
    await _teardown_on(
        path, sid, [await _deferred_queue(_RefusingDB(), logger)], status="completed"
    )

    row = await _session_row(path, sid)
    assert "8 live message event(s) were never written" in row["status_reason_summary"]
    assert len(row["status_evidence_refs"]) == 2, "one ref per queue that lost"


async def test_a_deferred_leg_that_lost_nothing_leaves_nothing_behind(tmp_path):
    """The control. Without it the resumed write could report a loss on every session
    that was ever deferred."""
    path = tmp_path / "deferred-clean.db"
    sid = "sess-deferred-clean"

    await _teardown_on(path, sid, [await _healthy_queue()], status="timed_out", defer_terminal=True)
    await _teardown_on(path, sid, [], status="completed")

    row = await _session_row(path, sid)
    assert row["status_reason_code"] == "run.completed.ok"
    assert row["status_evidence_refs"] in (None, [])


async def test_the_loss_is_recorded_on_a_run_that_failed_for_its_own_reasons(tmp_path):
    """The reason code belongs to the failure; the evidence still names the loss, or a
    reader can only find it on runs where nothing else went wrong."""
    out = await _closed_out(
        tmp_path,
        "sess-failed-with-loss",
        [await _deferred_queue(_RefusingDB(), logging.getLogger("test"))],
        status="failed",
        exception=RuntimeError("the real cause"),
    )

    assert out["row"]["status_reason_code"] == "run.failed.exception"
    kinds = {ref["kind"] for ref in (out["row"]["status_evidence_refs"] or [])}
    assert "message_persist_loss" in kinds


async def test_a_failed_loss_record_does_not_cost_the_deferred_leg_its_handoff(
    tmp_path, monkeypatch, caplog
):
    """The annotation must never decide whether a timed-out run gets resumed.

    The caller reads this status to choose the auto-resume path, so a bookkeeping
    write that raises here would trade a run that resumes for one that hangs
    unresumed. Losing the record is the lesser failure, and it is logged rather
    than swallowed because silent loss is what the record exists to stop.
    """
    from lionagi.state.db import StateDB

    path = tmp_path / "carry-fails.db"
    sid = "sess-carry-fails"
    logger = logging.getLogger("test")

    async def _refuse(self, *args, **kwargs):
        raise RuntimeError("node metadata write refused")

    monkeypatch.setattr(StateDB, "merge_session_node_metadata", _refuse)

    with caplog.at_level(logging.ERROR, logger="lionagi.cli"):
        final = await _teardown_on(
            path,
            sid,
            [await _deferred_queue(_RefusingDB(), logger)],
            status="timed_out",
            defer_terminal=True,
        )

    assert final == "timed_out", "the caller reads this to decide whether to resume"
    row = await _session_row(path, sid)
    assert row["status"] == "running", "the deferred leg still writes no terminal status"
    assert any("lost message event" in r.getMessage() for r in caplog.records)


# The carried loss payload crosses a persistence boundary, so its shape is an
# assumption. These pin what a drifted one is allowed to do to the count.


def _carry(payload: Any) -> dict[str, Any]:
    """A session row whose node metadata carries `payload` as the loss JSON."""
    from json import dumps

    return {"message_persist_loss_json": dumps(payload)}


def test_a_carried_payload_with_a_non_list_queues_field_is_dropped_not_walked():
    from lionagi.cli._runs import _merge_message_loss

    # list("ab") would otherwise yield character entries and q.get() would raise.
    merged = _merge_message_loss(_carry({"lost": 9, "queues": "ab"}), None)
    assert merged is None


def test_a_queue_entry_with_a_non_numeric_lost_is_dropped_rather_than_summed():
    from lionagi.cli._runs import _merge_message_loss

    merged = _merge_message_loss(
        _carry({"lost": 5, "queues": [{"owner": "a", "lost": "many"}, {"owner": "b", "lost": 2}]}),
        None,
    )
    assert merged == {"lost": 2, "queues": [{"owner": "b", "lost": 2}]}


def test_a_carried_total_that_disagrees_with_its_entries_is_recomputed_not_believed():
    from lionagi.cli._runs import _merge_message_loss

    merged = _merge_message_loss(_carry({"lost": 999, "queues": [{"owner": "a", "lost": 3}]}), None)
    assert merged["lost"] == 3, "the total must agree with the entries it claims to sum"


def test_a_boolean_lost_does_not_pass_as_a_count():
    from lionagi.cli._runs import _merge_message_loss

    assert _merge_message_loss(_carry({"queues": [{"owner": "a", "lost": True}]}), None) is None


def test_node_metadata_that_parses_to_a_non_mapping_does_not_raise():
    from lionagi.cli._runs import _merge_message_loss

    current = {"lost": 1, "queues": [{"owner": "live", "lost": 1}]}
    assert _merge_message_loss("[1, 2]", current) == current


def test_a_well_formed_carry_still_sums_with_this_legs_own_loss():
    from lionagi.cli._runs import _merge_message_loss

    merged = _merge_message_loss(
        _carry({"lost": 4, "queues": [{"owner": "deferred", "lost": 4}]}),
        {"lost": 1, "queues": [{"owner": "live", "lost": 1}]},
    )
    assert merged == {
        "lost": 5,
        "queues": [{"owner": "deferred", "lost": 4}, {"owner": "live", "lost": 1}],
    }
