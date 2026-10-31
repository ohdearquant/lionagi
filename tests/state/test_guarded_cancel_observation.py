# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Guarded session cancellation uses one consistent execution observation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing

import pytest

from lionagi.ln.concurrency import fail_after
from lionagi.state import db as db_module
from lionagi.state.compat import (
    CompatibilityProfile,
    StateCompatibilityRefused,
    _operation,
    read_closure,
)
from lionagi.state.db import StateDB
from lionagi.state.lifecycle import TerminalCallbackRegistry, adapters
from lionagi.state.lifecycle.models import (
    ActorRecord,
    ReasonRecord,
    TransitionCommand,
    TransitionOutcome,
)
from lionagi.state.reasons import PlayReasons, RunReasons


async def _seed(db: StateDB):
    session_id, generation, progression = (str(uuid.uuid4()) for _ in range(3))
    runner = {"pid": 32123, "pid_create_time": 123.5, "pid_host": "fixture-host"}
    await db.create_progression(progression)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": progression,
            "status": "running",
            "cli_worker_generation": generation,
            "node_metadata": runner,
        }
    )
    await db.update_session(session_id, current_phase="executing")
    return session_id, generation, runner


async def _cancel(db, session_id, generation, runner):
    return await db.cancel_session_if_worker_generation(
        session_id,
        generation,
        expected_runner_identity=runner,
        reason_code=RunReasons.CANCELLED_STALE_AUTO,
        reason_summary="runner is no longer live",
        evidence={"check": "runner-not-live"},
    )


async def _snapshot(db, session_id):
    return {
        "session": await db.get_session(session_id),
        "history": await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_type = 'session' AND entity_id = :id "
            "ORDER BY created_at, id",
            {"id": session_id},
        ),
        "audit": await db.fetch_all(
            "SELECT * FROM admin_events WHERE target_id = :id ORDER BY created_at, id",
            {"id": session_id},
        ),
    }


def _collect_terminal_callbacks(db):
    received = []
    registry = TerminalCallbackRegistry()
    registry.register("collector", lambda envelope: received.append(envelope))
    db._lifecycle_service()._terminal_callbacks = registry
    return received


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "reason"),
    [
        ("completed", RunReasons.COMPLETED_OK),
        ("failed", RunReasons.FAILED_EXCEPTION),
        ("cancelled", RunReasons.CANCELLED_SYSTEM),
    ],
)
@pytest.mark.parametrize("change", ["none", "generation", "runner", "both"])
async def test_terminal_row_reports_only_same_execution_as_already_terminal(
    tmp_path, terminal, reason, change
):
    async with StateDB(tmp_path / "terminal.db") as db:
        session_id, generation, runner = await _seed(db)
        await db.update_status("session", session_id, new_status=terminal, reason_code=reason)
        if change in ("generation", "both"):
            await db.update_session(session_id, cli_worker_generation=str(uuid.uuid4()))
        if change in ("runner", "both"):
            await db.update_session(session_id, node_metadata=json.dumps({**runner, "pid": 45678}))
        before = await _snapshot(db, session_id)
        received = _collect_terminal_callbacks(db)

        outcome = await _cancel(db, session_id, generation, runner)

        assert outcome == ("already_terminal" if change == "none" else "conflict")
        assert await _snapshot(db, session_id) == before
        assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "generation", "runner", "both"])
async def test_running_row_applies_only_to_the_matching_execution(tmp_path, change):
    async with StateDB(tmp_path / "running.db") as db:
        session_id, generation, runner = await _seed(db)
        if change in ("generation", "both"):
            await db.update_session(session_id, cli_worker_generation=str(uuid.uuid4()))
        if change in ("runner", "both"):
            await db.update_session(session_id, node_metadata=json.dumps({**runner, "pid": 45678}))
        before = await _snapshot(db, session_id)
        received = _collect_terminal_callbacks(db)

        outcome = await _cancel(db, session_id, generation, runner)
        after = await _snapshot(db, session_id)

        if change != "none":
            assert outcome == "conflict"
            assert after == before
            assert received == []
        else:
            assert outcome == "applied"
            assert after["session"]["status"] == "cancelled"
            assert after["session"]["current_phase"] is None
            assert len(after["history"]) == len(before["history"]) + 1
            assert after["audit"] == before["audit"]
            assert len(received) == 1
            assert received[0].terminal_status == "cancelled"
            assert await _cancel(db, session_id, generation, runner) == "already_terminal"
            assert await _snapshot(db, session_id) == after
            assert len(received) == 1


@pytest.mark.asyncio
async def test_missing_session_returns_missing_without_transition_or_event(tmp_path):
    async with StateDB(tmp_path / "missing.db") as db:
        session_id, generation, runner = await _seed(db)
        missing = str(uuid.uuid4())
        before = await _snapshot(db, session_id)
        missing_before = await _snapshot(db, missing)
        received = _collect_terminal_callbacks(db)

        assert await _cancel(db, missing, generation, runner) == "missing"
        assert await _snapshot(db, session_id) == before
        assert await _snapshot(db, missing) == missing_before
        assert received == []


@pytest.mark.asyncio
async def test_matching_terminal_cancel_classification_uses_one_transaction(tmp_path, monkeypatch):
    """The guarded cancel classifies its terminal conflict within the service transaction."""
    async with StateDB(tmp_path / "one-transaction.db") as db:
        session_id, generation, runner = await _seed(db)
        await db.update_status(
            "session", session_id, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
        before = await _snapshot(db, session_id)
        received = _collect_terminal_callbacks(db)
        transactions = []
        original_tx = db._tx

        def count_tx(*args, **kwargs):
            transactions.append(True)
            return original_tx(*args, **kwargs)

        # Count only this cancellation call; setup and assertions may use
        # their own database operations outside the measurement window.
        with monkeypatch.context() as scoped:
            scoped.setattr(db, "_tx", count_tx)
            outcome = await _cancel(db, session_id, generation, runner)

        assert outcome == "already_terminal"
        assert await _snapshot(db, session_id) == before
        assert received == []
        assert transactions == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_at_first_read", [True, False])
async def test_terminal_classification_uses_first_observation_after_row_changes(
    tmp_path, monkeypatch, matching_at_first_read
):
    path = tmp_path / "race.db"
    async with StateDB(path) as db, StateDB(path) as writer:
        session_id, generation, runner = await _seed(db)
        await db.update_status(
            "session", session_id, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
        other_generation = str(uuid.uuid4())
        if not matching_at_first_read:
            await writer.update_session(session_id, cli_worker_generation=other_generation)
        received = _collect_terminal_callbacks(db)
        service = db._lifecycle_service()
        transition = service._transition
        observed = asyncio.Event()
        resume = asyncio.Event()

        async def hold_after_transition(*args, **kwargs):
            outcome = await transition(*args, **kwargs)
            assert outcome.result == "conflict" and outcome.previous_status == "failed"
            observed.set()
            await resume.wait()
            return outcome

        with monkeypatch.context() as scoped:
            scoped.setattr(service, "_transition", hold_after_transition)
            task = asyncio.create_task(_cancel(db, session_id, generation, runner))
            try:
                await asyncio.wait_for(observed.wait(), timeout=5)
                await writer.update_session(
                    session_id,
                    cli_worker_generation=(
                        other_generation if matching_at_first_read else generation
                    ),
                )
                expected_state = await _snapshot(writer, session_id)
                resume.set()
                outcome = await asyncio.wait_for(task, timeout=5)
                assert outcome == ("already_terminal" if matching_at_first_read else "conflict")
                assert await _snapshot(db, session_id) == expected_state
                assert received == []
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_guarded_cancels_keep_observations_per_call(tmp_path, monkeypatch):
    async with StateDB(tmp_path / "concurrent.db") as db:
        first_id, first_generation, first_runner = await _seed(db)
        second_id, second_generation, second_runner = await _seed(db)
        for session_id in (first_id, second_id):
            await db.update_status(
                "session", session_id, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
            )
        await db.update_session(second_id, cli_worker_generation=str(uuid.uuid4()))
        before = [await _snapshot(db, sid) for sid in (first_id, second_id)]
        received = _collect_terminal_callbacks(db)
        service = db._lifecycle_service()
        transition = service._transition
        observed = 0
        both_observed, release = asyncio.Event(), asyncio.Event()

        async def hold_after_transition(*args, **kwargs):
            nonlocal observed
            outcome = await transition(*args, **kwargs)
            observed += 1
            if observed == 2:
                both_observed.set()
            await release.wait()
            return outcome

        with monkeypatch.context() as scoped:
            scoped.setattr(service, "_transition", hold_after_transition)
            tasks = [
                asyncio.create_task(_cancel(db, first_id, first_generation, first_runner)),
                asyncio.create_task(_cancel(db, second_id, second_generation, second_runner)),
            ]
            try:
                await asyncio.wait_for(both_observed.wait(), timeout=5)
                release.set()
                assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=5) == [
                    "already_terminal",
                    "conflict",
                ]
                assert [await _snapshot(db, sid) for sid in (first_id, second_id)] == before
                assert received == []
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_one_guarded_call_does_not_affect_another(tmp_path, monkeypatch):
    async with StateDB(tmp_path / "cancelled-observer.db") as db:
        first_id, first_generation, first_runner = await _seed(db)
        second_id, second_generation, second_runner = await _seed(db)
        for session_id in (first_id, second_id):
            await db.update_status(
                "session", session_id, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
            )
        await db.update_session(second_id, cli_worker_generation=str(uuid.uuid4()))
        before = [await _snapshot(db, sid) for sid in (first_id, second_id)]
        received = _collect_terminal_callbacks(db)
        service = db._lifecycle_service()
        transition = service._transition
        observed = 0
        both_observed, release = asyncio.Event(), asyncio.Event()

        async def hold_after_transition(*args, **kwargs):
            nonlocal observed
            outcome = await transition(*args, **kwargs)
            observed += 1
            if observed == 2:
                both_observed.set()
            await release.wait()
            return outcome

        with monkeypatch.context() as scoped:
            scoped.setattr(service, "_transition", hold_after_transition)
            first = asyncio.create_task(_cancel(db, first_id, first_generation, first_runner))
            second = asyncio.create_task(_cancel(db, second_id, second_generation, second_runner))
            try:
                await asyncio.wait_for(both_observed.wait(), timeout=5)
                first.cancel()
                first_result = await asyncio.gather(first, return_exceptions=True)
                assert isinstance(first_result[0], asyncio.CancelledError)
                release.set()
                assert await asyncio.wait_for(second, timeout=5) == "conflict"
                assert [await _snapshot(db, sid) for sid in (first_id, second_id)] == before
                assert received == []
            finally:
                release.set()
                for task in (first, second):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["session", "generation", "identity", "reason"])
async def test_direct_guarded_service_rejects_invalid_inputs_without_writes(
    tmp_path, monkeypatch, invalid
):
    async with StateDB(tmp_path / "direct-invalid.db") as db:
        session_id, generation, runner = await _seed(db)
        before = await _snapshot(db, session_id)
        received = _collect_terminal_callbacks(db)
        arguments = {
            "session_id": session_id,
            "generation_id": generation,
            "expected_runner_identity": runner,
            "reason": ReasonRecord(code=RunReasons.CANCELLED_STALE_AUTO),
        }
        if invalid == "session":
            arguments["session_id"] = ""
        elif invalid == "generation":
            arguments["generation_id"] = "not-a-uuid"
        elif invalid == "identity":
            arguments["expected_runner_identity"] = []
        else:
            arguments["reason"] = ReasonRecord(code="not_a_registered_reason")

        def unexpected_transaction(*_args, **_kwargs):
            pytest.fail("invalid arguments must be rejected before opening a transaction")

        with monkeypatch.context() as scoped:
            scoped.setattr(db, "_tx", unexpected_transaction)
            with pytest.raises(ValueError):
                await db._lifecycle_service().guarded_cancel_session(**arguments)
        assert await _snapshot(db, session_id) == before
        assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["compatibility", "compatibility-scoped", "readonly"])
async def test_direct_guarded_service_refuses_non_native_store_before_open(tmp_path, mode):
    path = tmp_path / f"{mode}.db"
    if mode.startswith("compatibility"):
        with closing(sqlite3.connect(":memory:")) as reference:
            closure = read_closure(reference, [])
        profile = CompatibilityProfile(
            name="cleanup-refusal",
            version=1,
            closure=closure,
            writes={},
            operations=frozenset({"cancel_session_if_worker_generation"}),
        )
        db = StateDB(path, compatibility=profile)
        error_type = StateCompatibilityRefused
    else:
        db = StateDB(path, readonly=True)
        error_type = ValueError

    async def invoke(state):
        return await state._lifecycle_service().guarded_cancel_session(
            session_id="session",
            generation_id=None,
            expected_runner_identity={},
            reason=ReasonRecord(code=RunReasons.CANCELLED_STALE_AUTO),
        )

    scoped_invoke = _operation("cancel_session_if_worker_generation")(invoke)
    with pytest.raises(error_type) as exc:
        if mode == "compatibility-scoped":
            await scoped_invoke(db)
        else:
            await invoke(db)
    if mode.startswith("compatibility"):
        assert exc.value.operation == "cancel_session_if_worker_generation"
    assert db._engine is None
    assert not path.exists()


@pytest.mark.asyncio
async def test_direct_guarded_service_keeps_facade_generation_and_reason_rules(tmp_path):
    async with StateDB(tmp_path / "facade-rules.db") as db:
        session_id, generation, runner = await _seed(db)
        await db.update_status(
            "session", session_id, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
        before = await _snapshot(db, session_id)
        # The facade accepts any registered reason, not only a session-prefixed
        # one, and normalizes UUID text before comparing the generation.
        outcome, same = await db._lifecycle_service().guarded_cancel_session(
            session_id=session_id,
            generation_id=generation.upper(),
            expected_runner_identity=runner,
            reason=ReasonRecord(code=PlayReasons.PENDING_CREATED),
        )
        assert outcome.result == "conflict" and same is True
        assert await _snapshot(db, session_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["worker_deadline", "caller_deadline", "task_cancel"])
async def test_direct_guarded_service_deadline_and_cancellation_leave_no_writes(
    tmp_path, monkeypatch, interruption
):
    async with StateDB(tmp_path / "direct-timeout.db") as db:
        session_id, generation, runner = await _seed(db)
        before = await _snapshot(db, session_id)
        received = _collect_terminal_callbacks(db)
        service = db._lifecycle_service()
        transition = service._transition
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(
            db_module,
            "_CLI_WORKER_OPERATION_TIMEOUT_SECONDS",
            0.2 if interruption == "worker_deadline" else 5.0,
        )

        async def held_transition(*args, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await transition(*args, **kwargs)

        async def invoke():
            async def call():
                return await service.guarded_cancel_session(
                    session_id=session_id,
                    generation_id=generation,
                    expected_runner_identity=runner,
                    reason=ReasonRecord(code=RunReasons.CANCELLED_STALE_AUTO),
                )

            if interruption == "caller_deadline":
                with fail_after(0.2):
                    return await call()
            return await call()

        with monkeypatch.context() as scoped:
            scoped.setattr(service, "_transition", held_transition)
            task = asyncio.create_task(invoke())
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                if interruption == "task_cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(task, timeout=5)
                assert cancelled.is_set()
                release.set()
                await asyncio.sleep(0)
                assert await _snapshot(db, session_id) == before
                assert received == []
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_generic_transition_conflict_retains_public_outcome(tmp_path):
    async with StateDB(tmp_path / "generic.db") as db:
        session_id, _generation, _runner = await _seed(db)
        await db.update_status(
            "session", session_id, new_status="failed", reason_code=RunReasons.FAILED_EXCEPTION
        )
        before = await _snapshot(db, session_id)
        outcome = await db._lifecycle_service().transition(
            TransitionCommand(
                entity_type="session",
                entity_id=session_id,
                to_status="cancelled",
                reason=ReasonRecord(code=RunReasons.CANCELLED_STALE_AUTO),
                actor=ActorRecord(type="admin", id="user"),
                expected_statuses=frozenset({"running"}),
            )
        )
        assert type(outcome) is TransitionOutcome
        assert outcome == TransitionOutcome("conflict", "failed", "failed", None)
        assert await _snapshot(db, session_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "same", "expected"),
    [
        ("applied", False, "applied"),
        ("rejected", False, "rejected"),
        ("conflict", False, "conflict"),
        ("conflict", True, "already_terminal"),
    ],
)
async def test_cancel_adapter_uses_public_service_seam(result, same, expected):
    class PublicOnlyService:
        async def guarded_cancel_session(self, **kwargs):
            assert kwargs["session_id"] == "s1"
            assert kwargs["generation_id"] == "g1"
            assert kwargs["expected_runner_identity"] == {"pid": 42}
            return TransitionOutcome(result, "failed", "failed", None), same

        def __getattr__(self, name):
            raise AssertionError(f"unexpected service attribute: {name}")

    assert (
        await adapters.run_cancel_session_if_worker_generation(
            PublicOnlyService(),
            session_id="s1",
            generation_id="g1",
            expected_runner_identity={"pid": 42},
            reason_code=RunReasons.CANCELLED_STALE_AUTO,
            reason_summary="stale",
            evidence={"check": "dead"},
        )
        == expected
    )
