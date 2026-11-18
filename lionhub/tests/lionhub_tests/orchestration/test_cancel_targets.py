# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared-grace and settlement tests with a synthetic process table."""

from __future__ import annotations

import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.ln import _proc
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionhub.orchestration import kill
from lionhub.orchestration.kill import CancelPersistenceError, cancel_targets


def _target(kind, entity_id, pid):
    return (
        kind,
        entity_id,
        {
            "node_metadata": {"pid": pid, "pid_create_time": 100.0},
            "cli_worker_generation": None,
        },
    )


def _command_target(pid: int = 71031):
    return (
        "invocation",
        "command",
        {
            "node_metadata": {
                "action_kind": "command",
                "pid": pid,
                "pgid": pid,
                "pid_create_time": 100.0,
                "pid_host": "fixture-host",
                "pid_boot_time": 900.0,
                "process_identity_mode": "local",
            }
        },
    )


def _command_kernel(monkeypatch, behavior):
    leader, child = 71031, 71032
    births = {leader: 100.0, child: 101.0}
    attempted = []
    delivered = []

    def no_signal(*_args):
        raise AssertionError("unexpected signal or generic PID termination")

    def read_birth(pid, *, expected_created=None):
        raw = births.get(pid)
        return ("gone", None) if raw is None else ("found", raw)

    def get_group(pid):
        if pid not in births:
            raise ProcessLookupError
        return leader

    def read_group(pgid):
        assert pgid == leader
        return list(births.items()), True

    def send_group(pgid, sig):
        assert pgid == leader
        attempted.append(sig)
        if sig == signal.SIGTERM:
            del births[leader]
            if behavior == "term":
                births.clear()
            elif behavior == "replaced":
                births[child] = 200.0
        else:
            assert sig == signal.SIGKILL
            assert births == {child: 101.0}
            if behavior == "permission":
                raise PermissionError
            births.clear()
            if behavior == "vanished":
                raise ProcessLookupError
        delivered.append(sig)

    monkeypatch.setattr(_proc, "_recorded_process_create_time", read_birth)
    monkeypatch.setattr(_proc, "_group_member_identities", read_group)
    monkeypatch.setattr(
        _proc, "os", SimpleNamespace(getpgrp=lambda: 20, getpgid=get_group, killpg=no_signal)
    )
    monkeypatch.setattr(_proc, "socket", SimpleNamespace(gethostname=lambda: "fixture-host"))
    monkeypatch.setattr(kill.psutil, "boot_time", lambda: 900.0)
    monkeypatch.setattr(kill, "os", SimpleNamespace(kill=no_signal, killpg=send_group))
    monkeypatch.setattr(kill, "_terminate_pid", no_signal)
    return births, attempted, delivered


@pytest.mark.parametrize("studio", [False, True], ids=["cli", "studio"])
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behavior", "outcome", "did_attempt_kill", "did_deliver_kill", "reason"),
    [
        ("term", "sigterm", False, False, RunReasons.CANCELLED_MANUAL_KILL),
        ("kill", "sigkill", True, True, RunReasons.CANCELLED_FORCE_KILL),
        ("vanished", "already_dead", True, False, RunReasons.CANCELLED_MANUAL_KILL),
        ("permission", "permission_denied", True, False, None),
        ("replaced", "identity_unverifiable_after_sigterm", False, False, None),
    ],
)
async def test_scheduled_command_cancels_the_verified_group(
    monkeypatch, studio, behavior, outcome, did_attempt_kill, did_deliver_kill, reason
):
    births, attempted, delivered = _command_kernel(monkeypatch, behavior)
    target = _command_target()
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"status": "running"}),
        update_status=AsyncMock(return_value=True),
    )
    if studio:
        (result,) = await cancel_targets(db, [target], user_reason="", grace_seconds=0)
        assert (result.signal, result.pgid, result.signals_sent) == (
            outcome,
            71031,
            tuple(delivered),
        )
        assert result.state_committed is (reason is not None)
    else:
        result = await kill.kill_one(db, *target, user_reason="", grace_seconds=0)
        assert (result["signal"], result["pgid"]) == (outcome, 71031)

    assert attempted == [signal.SIGTERM] + ([signal.SIGKILL] if did_attempt_kill else [])
    assert delivered == [signal.SIGTERM] + ([signal.SIGKILL] if did_deliver_kill else [])
    if reason is None:
        db.update_status.assert_not_awaited()
        assert births
    else:
        db.update_status.assert_awaited_once()
        assert db.update_status.call_args.kwargs["reason_code"] == reason
        (evidence,) = db.update_status.call_args.kwargs["evidence_refs"]
        assert (evidence["signal"], evidence["pgid"]) == (outcome, 71031)
        assert not births


def test_scheduled_command_group_inspection_is_read_only(monkeypatch):
    births, attempted, delivered = _command_kernel(monkeypatch, "kill")
    kind, _, row = _command_target()
    meta = row["node_metadata"]
    inspect = kill._inspect_cancel_command_group
    assert inspect("session", row) is None
    assert inspect(kind, row).outcome == "verified"
    assert inspect(kind, {"node_metadata": {"action_kind": "command"}}).outcome == "no_pid"
    assert (
        inspect(kind, {"node_metadata": {**meta, "pgid": None}}).outcome == "identity_unverifiable"
    )
    assert inspect(kind, {"node_metadata": {**meta, "pgid": 20}}).outcome == "unsafe_group"
    assert (
        inspect(kind, {"node_metadata": {**meta, "pid_host": "another-host"}}).outcome
        == "host_mismatch"
    )
    del births[71031]
    assert inspect(kind, row).outcome == "identity_unverifiable"
    births.clear()
    assert inspect(kind, row).outcome == "already_dead"
    monkeypatch.setattr(kill, "group_member_pids", lambda _pgid: ([], False))
    assert inspect(kind, row).outcome == "identity_unverifiable"
    assert attempted == delivered == []


@pytest.mark.parametrize(
    "publish_before_cancel", [False, True], ids=["legacy", "racing-publication"]
)
@pytest.mark.asyncio
async def test_no_pid_invocation_cancel_uses_the_resolved_row_version(
    tmp_path, publish_before_cancel
):
    kind, entity_id, launched = _command_target()
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_invocation(
            {"id": entity_id, "skill": "command", "status": "running", "started_at": 100.0}
        )
        before = await db.get_invocation(entity_id)
        if publish_before_cancel:
            await db.merge_invocation_node_metadata(entity_id, launched["node_metadata"])
            current = await db.get_invocation(entity_id)
            assert current["updated_at"] != before["updated_at"]

        result = await kill.kill_one(db, kind, entity_id, before, user_reason="")
        assert result["signal"] == "no_pid"
        after = await db.get_invocation(entity_id)
        if publish_before_cancel:
            assert result.get("cancel_status") == "conflict"
            assert after == current
        else:
            assert "cancel_error" not in result
            assert after["status"] == "cancelled"


@pytest.mark.asyncio
async def test_scheduled_command_group_shares_grace_with_pid_targets(monkeypatch):
    _, attempted, _ = _command_kernel(monkeypatch, "kill")
    command_signal = kill.os.killpg
    command_pid, generic_pid = 71031, 71041
    signalled = []
    clock = [0.0]
    terms = [(command_pid, signal.SIGTERM, 0.0), (generic_pid, signal.SIGTERM, 0.0)]

    def send_group(pid, sig):
        signalled.append((pid, sig, clock[0]))
        command_signal(pid, sig)

    def send_pid(pid, sig):
        assert pid == generic_pid
        signalled.append((pid, sig, clock[0]))

    def sleep(seconds):
        assert signalled == terms
        clock[0] += seconds

    monkeypatch.setattr(kill, "os", SimpleNamespace(kill=send_pid, killpg=send_group))
    monkeypatch.setattr(
        kill, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep, time=lambda: 1234)
    )
    monkeypatch.setattr(kill, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kill, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(kill, "check_pid_identity", lambda *_args, **_kwargs: "ours")
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"status": "running"}),
        update_status=AsyncMock(return_value=True),
    )
    results = await cancel_targets(
        db,
        [_command_target(), _target("invocation", "generic", generic_pid)],
        user_reason="",
        grace_seconds=1.0,
    )

    assert attempted == [signal.SIGTERM, signal.SIGKILL]
    assert signalled == [
        *terms,
        (command_pid, signal.SIGKILL, 1.0),
        (generic_pid, signal.SIGKILL, 1.0),
    ]
    assert [result.signal for result in results] == ["sigkill", "sigkill"]
    assert all(result.state_committed for result in results)


@pytest.mark.asyncio
async def test_all_terms_precede_one_grace_and_escalation_revalidates_identity(monkeypatch):
    targets = [
        _target("invocation", "deepest", 71001),
        _target("invocation", "exits", 71002),
        _target("invocation", "replaced", 71003),
        _target("session", "parent", 71004),
    ]
    parent_row = targets[-1][2]
    parent_row["node_metadata"]["pid_host"] = None
    signalled = []
    signals_at = []
    checked = []
    clock = [0.0]
    terms = [(pid, signal.SIGTERM) for pid in range(71001, 71005)]

    def send(pid, sig):
        signalled.append((pid, sig))
        signals_at.append(clock[0])
        if sig == signal.SIGTERM:
            clock[0] += 0.05
        if (pid, sig) == terms[0]:
            # The latest row is no longer the snapshot under which we signalled.
            parent_row["cli_worker_generation"] = "new-worker"
            parent_row["node_metadata"]["pid_create_time"] = 200.0
            parent_row["node_metadata"]["pid_boot_time"] = 300.0

    def sleep(seconds):
        assert signalled == terms
        clock[0] += seconds

    def identity(pid, _command, **kwargs):
        checked.append((pid, kwargs["expected_create_time"]))
        if pid == 71003 and (pid, signal.SIGTERM) in signalled:
            return "not_ours"
        return "ours"

    monkeypatch.setattr(kill, "os", SimpleNamespace(kill=send))
    monkeypatch.setattr(
        kill, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep, time=lambda: 1234)
    )
    monkeypatch.setattr(kill, "_pid_alive", lambda pid: pid != 71002 or terms[1] not in signalled)
    monkeypatch.setattr(kill, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(kill, "check_pid_identity", identity)

    writes = []

    async def write_invocation(_kind, entity_id, **_kwargs):
        writes.append(entity_id)
        return True

    async def write_guarded_session(entity_id, generation, *, expected_runner_identity, **_kwargs):
        writes.append(entity_id)
        assert generation is None
        assert expected_runner_identity == {
            "pid": 71004,
            "pid_create_time": 100.0,
            "pid_host": None,
        }
        return "applied"

    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"status": "running"}),
        update_status=write_invocation,
        cancel_session_if_worker_generation=write_guarded_session,
    )
    results = await cancel_targets(db, targets, user_reason="test", grace_seconds=1.0)

    assert signalled == [*terms, (71001, signal.SIGKILL), (71004, signal.SIGKILL)]
    assert signals_at == pytest.approx([0.0, 0.05, 0.10, 0.15, 1.20, 1.20])
    assert checked == [(pid, 100.0) for pid in (71001, 71002, 71003, 71004, 71001, 71003, 71004)]
    assert writes == ["deepest", "exits", "parent"]
    assert [result.signal for result in results] == [
        "sigkill",
        "sigterm",
        "identity_mismatch",
        "sigkill",
    ]
    assert [result.state_committed for result in results] == [True, True, False, True]
    assert results[2].signals_sent == (signal.SIGTERM,)


@pytest.mark.parametrize("degraded", [False, True])
@pytest.mark.asyncio
async def test_unrepresentable_pid_refuses_only_its_target(monkeypatch, tmp_path, degraded):
    oversized_pid = 2**31
    targets = [
        _target("invocation", "first", 71001),
        _target("session", "refused", None if degraded else oversized_pid),
        _target("session", "last", 71003),
    ]
    if degraded:
        monkeypatch.setattr(
            kill,
            "_degraded_run",
            lambda _entity_id: (
                tmp_path / "run.json",
                {"pid": oversized_pid, "pid_create_time": 100.0},
            ),
        )
    events = []

    def alive(pid):
        if pid == oversized_pid:
            raise OverflowError("PID outside the supported range")
        return ("signal", pid, signal.SIGTERM) not in events

    def send(pid, sig):
        events.append(("signal", pid, sig))

    async def write_invocation(_kind, entity_id, **_kwargs):
        events.append(("write", entity_id))
        return True

    async def write_session(entity_id, _generation, **_kwargs):
        events.append(("write", entity_id))
        return "applied"

    monkeypatch.setattr(kill, "os", SimpleNamespace(kill=send))
    monkeypatch.setattr(kill, "_pid_alive", alive)
    monkeypatch.setattr(kill, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(kill, "check_pid_identity", lambda *_args, **_kwargs: "ours")
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"status": "running"}),
        update_status=write_invocation,
        cancel_session_if_worker_generation=write_session,
    )

    first, refused, last = await cancel_targets(db, targets, user_reason="", grace_seconds=0.0)

    assert events == [
        ("signal", 71001, signal.SIGTERM),
        ("signal", 71003, signal.SIGTERM),
        ("write", "first"),
        ("write", "last"),
    ]
    assert first.state_committed and first.signals_sent == (signal.SIGTERM,)
    assert refused.signal == "identity_unverifiable"
    assert refused.pid == oversized_pid
    assert refused.signals_sent == ()
    assert not refused.state_committed
    assert last.state_committed and last.signals_sent == (signal.SIGTERM,)


@pytest.mark.asyncio
async def test_first_write_failure_carries_later_target_effects(monkeypatch):
    targets = [
        _target("invocation", "first", 71001),
        _target("invocation", "second", 71002),
        _target("session", "parent", 71003),
    ]
    sent = []
    monkeypatch.setattr(kill, "os", SimpleNamespace(kill=lambda pid, sig: sent.append((pid, sig))))
    monkeypatch.setattr(kill, "_pid_alive", lambda pid: pid == 71003 and not sent)
    monkeypatch.setattr(kill, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(kill, "check_pid_identity", lambda *_args, **_kwargs: "ours")
    first_failure = OSError("first write failed")
    attempts = []

    async def fail_write(_kind, entity_id, **_kwargs):
        attempts.append(entity_id)
        if entity_id == "first":
            raise first_failure
        raise TimeoutError("second write failed")

    async def apply_session(entity_id, _generation, **_kwargs):
        attempts.append(entity_id)
        return "applied"

    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"status": "running"}),
        update_status=fail_write,
        cancel_session_if_worker_generation=apply_session,
    )
    with pytest.raises(CancelPersistenceError) as captured:
        await cancel_targets(db, targets, user_reason="", grace_seconds=0.0)

    error = captured.value
    assert error.__cause__ is first_failure
    assert (error.entity_type, error.entity_id) == ("invocation", "first")
    assert attempts == ["first", "second", "parent"]
    assert sent == [(71003, signal.SIGTERM)]
    assert error.signal_sent
    assert [result.cancel_status for result in error.results] == [
        "write_failed",
        "write_failed",
        "applied",
    ]
    assert [result.signal_sent for result in error.results] == [False, False, True]
    assert [result.state_committed for result in error.results] == [False, False, True]


@pytest.mark.asyncio
async def test_refused_escalation_retains_term_effect_without_a_state_write(monkeypatch):
    sent = []

    def send(pid, sig):
        if sig == signal.SIGKILL:
            raise PermissionError("escalation denied")
        sent.append((pid, sig))

    monkeypatch.setattr(kill, "os", SimpleNamespace(kill=send))
    monkeypatch.setattr(kill, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kill, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(kill, "check_pid_identity", lambda *_args, **_kwargs: "ours")
    db = SimpleNamespace(cancel_session_if_worker_generation=AsyncMock())
    (result,) = await cancel_targets(
        db, [_target("session", "parent", 71004)], user_reason="", grace_seconds=0.0
    )

    assert result.signal == "permission_denied"
    assert result.signals_sent == (signal.SIGTERM,)
    assert sent == [(71004, signal.SIGTERM)]
    assert not result.state_committed
    db.cancel_session_if_worker_generation.assert_not_awaited()
