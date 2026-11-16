# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared-grace and settlement tests with a synthetic process table."""

from __future__ import annotations

import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
