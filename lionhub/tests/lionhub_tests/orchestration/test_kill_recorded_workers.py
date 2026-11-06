# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Recorded worker-group cleanup service guards."

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from lionagi.ln.concurrency import sleep as _real_sleep
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._kill_recorded_workers_test_support import _CleanupDB as _CleanupDB
from tests._kill_recorded_workers_test_support import _stub_recorded_groups as _stub_recorded_groups
from tests._kill_scope_test_support import (
    _guard_runner_probes_in_kill_tests as _guard_runner_probes_in_kill_tests,
)


@pytest.mark.asyncio
async def test_guard_failure_stops_remaining_groups_and_all_settlement(monkeypatch):
    import lionhub.orchestration.kill as module

    db = _CleanupDB(guards=("unchanged", TimeoutError("guard deadline")))
    sent = _stub_recorded_groups(monkeypatch, module)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == [(76541, signal.SIGTERM)]
    assert db.settled == []


@pytest.mark.asyncio
async def test_interruption_during_grace_propagates_without_kill_or_settlement(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)

    async def interrupted(_delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr(kill_service, "sleep", interrupted)
    before = dict(db.session)
    with pytest.raises(asyncio.CancelledError):
        await module._try_recorded_worker_cleanup(db, db.session, user_reason="", grace_seconds=1)
    assert sent == [(76541, signal.SIGTERM), (76542, signal.SIGTERM)]
    assert db.settled == []
    assert db.session == before


@pytest.mark.asyncio
async def test_resume_race_cancellation_conflict_never_signals_workers(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running", outcome="conflict")
    sent = _stub_recorded_groups(monkeypatch, module)
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: ("gone", None))
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == [("session-test", "generation-a")]
    assert db.cancel_identities == [{"pid": 76543}]
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_cancellation_deadline_is_reported_and_never_signals_workers(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")

    async def deadline_passed(session_id, generation, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(db, "cancel_session_if_worker_generation", deadline_passed)
    sent = _stub_recorded_groups(monkeypatch, module)
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: ("gone", None))
    errors: list[str] = []
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(kill_service, "log_error", errors.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert errors == ["session session-test cancellation did not finish before the deadline"]
    assert db.session["status"] == "running"
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_cancellation_captures_only_present_runner_markers(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running", outcome="conflict")
    db.session["node_metadata"] = {
        "pid": 76543,
        "pid_create_time": None,
        "pid_host": "worker-host",
        "process_identity_mode": "local",
        "unrelated": "not part of the runner identity",
    }
    sent = _stub_recorded_groups(monkeypatch, module)
    patched_unaddressable_pid_reason = lambda meta: None
    monkeypatch.setattr(module, "unaddressable_pid_reason", patched_unaddressable_pid_reason)
    monkeypatch.setattr(kill_service, "unaddressable_pid_reason", patched_unaddressable_pid_reason)
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: ("gone", None))
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancel_identities == [
        {
            "pid": 76543,
            "pid_create_time": None,
            "pid_host": "worker-host",
            "process_identity_mode": "local",
        }
    ]
    assert "pid_boot_time" not in db.cancel_identities[0]
    assert "unrelated" not in db.cancel_identities[0]
    assert sent == []
    assert db.settled == []


@pytest.mark.parametrize("invalid_metadata", [None, [], "not-json"])
@pytest.mark.asyncio
async def test_unusable_snapshot_metadata_cannot_cancel_or_clean_workers(
    monkeypatch, invalid_metadata
):
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = invalid_metadata
    sent = _stub_recorded_groups(monkeypatch, module)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_missing_runner_row_identity_cannot_cancel_away_recorded_workers(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {}
    db.session["artifacts_path"] = "/outside-the-test"
    sent = _stub_recorded_groups(monkeypatch, module)
    warnings = []
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []
    assert any(
        "worker group for session session-test could not be confirmed stopped" in msg
        for msg in warnings
    )


@pytest.mark.parametrize("invalid_pid", [76543.5, float("inf"), 2**31, 10**50, str(10**50), True])
@pytest.mark.asyncio
async def test_invalid_runner_pid_cannot_probe_cancel_or_clean_workers(monkeypatch, invalid_pid):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {"pid": invalid_pid}
    sent = _stub_recorded_groups(monkeypatch, module)
    warnings = []
    probes = []
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: probes.append(pid))
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert probes == []
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []
    assert any(
        "worker group for session session-test could not be confirmed stopped" in msg
        for msg in warnings
    )


@pytest.mark.asyncio
async def test_nan_runner_pid_cannot_probe_cancel_or_clean_workers(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    db.session["node_metadata"] = {"pid": float("nan")}
    sent = _stub_recorded_groups(monkeypatch, module)
    probes = []
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: probes.append(pid))
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert probes == []
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []


@pytest.mark.parametrize("valid_pid", [76543, 76543.0, "76543", " +76543 "])
@pytest.mark.asyncio
async def test_integer_runner_pid_forms_are_preserved(monkeypatch, valid_pid):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running", outcome="conflict")
    db.session["node_metadata"] = {"pid": valid_pid}
    sent = _stub_recorded_groups(monkeypatch, module)
    probes = []

    def dead_runner(pid):
        probes.append(pid)
        return "gone", None

    monkeypatch.setattr(kill_service, "_runner_process_create_time", dead_runner)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert probes == [76543]
    assert db.cancelled == [("session-test", "generation-a")]
    assert db.cancel_identities == [{"pid": valid_pid}]
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_unreadable_runner_cannot_cancel_away_recorded_workers(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    sent = _stub_recorded_groups(monkeypatch, module)
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: ("unknown", None))
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.cancelled == []
    assert sent == []
    assert db.settled == []


@pytest.mark.asyncio
async def test_all_terms_precede_one_grace_then_exact_settlement(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    waits = []

    async def one_wait(delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        waits.append(delay)

    monkeypatch.setattr(kill_service, "sleep", one_wait)
    monkeypatch.setattr(
        kill_service,
        "_recorded_worker_group_matches",
        lambda witness: (
            "missing" if any(pid == witness.identity.pgid for pid, _ in sent) else "matches"
        ),
    )
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0.5
    )
    assert result == 0
    assert sent == [(76541, signal.SIGTERM), (76542, signal.SIGTERM)]
    assert waits == [0.5]
    assert db.settled == [record["spawn_id"] for record in db.records]


@pytest.mark.asyncio
async def test_missing_killpg_refuses_without_settling(monkeypatch):
    import lionhub.orchestration.kill as module

    db = _CleanupDB()
    monkeypatch.delattr(os, "killpg")
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.settled == []


@pytest.mark.asyncio
async def test_all_refused_groups_still_report_a_concurrent_generation_change(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal

    db = _CleanupDB()
    original = dict(db.session)
    errors = []
    sent = []

    def refuse(identity, **kwargs):
        db.session["cli_worker_generation"] = "generation-b"
        return _RecordedGroupRefusal("host_mismatch")

    monkeypatch.setattr(kill_service, "_admit_recorded_worker_group", refuse)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(kill_service, "log_error", errors.append)
    result = await module._try_recorded_worker_cleanup(
        db, original, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == []
    assert db.settled == []
    assert any("changed during cleanup" in message for message in errors)


@pytest.mark.asyncio
async def test_one_malformed_record_produces_one_identity_warning(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB()
    db.records = [dict(db.records[0])]
    del db.records[0]["pid"]
    warnings = []
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result == 1
    assert db.settled == []
    assert sum("recorded worker identity" in message for message in warnings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["host_mismatch", "in_process", "foreign_mode"])
async def test_recorded_workers_of_a_runner_this_host_cannot_judge_are_reported(
    monkeypatch, reason
):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    warnings = []
    patched_unaddressable_pid_reason = lambda meta: reason
    monkeypatch.setattr(module, "unaddressable_pid_reason", patched_unaddressable_pid_reason)
    monkeypatch.setattr(kill_service, "unaddressable_pid_reason", patched_unaddressable_pid_reason)
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result is None
    assert db.cancelled == []
    assert db.settled == []
    [message] = [m for m in warnings if "recorded worker group(s) not handled" in m]
    assert "2 recorded worker group(s)" in message
    assert module._REFUSED_SIGNAL_REASONS[reason] in message


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "records"), [("boot_mismatch", True), ("host_mismatch", False)])
async def test_no_unhandled_worker_report_without_live_recorded_workers(
    monkeypatch, reason, records
):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    if not records:
        db.records = []
    warnings = []
    patched_unaddressable_pid_reason = lambda meta: reason
    monkeypatch.setattr(module, "unaddressable_pid_reason", patched_unaddressable_pid_reason)
    monkeypatch.setattr(kill_service, "unaddressable_pid_reason", patched_unaddressable_pid_reason)
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )
    assert result is None
    assert not [m for m in warnings if "not handled" in m]


@pytest.mark.asyncio
async def test_running_dead_runner_resumed_during_grace_settles_old_group(monkeypatch, capsys):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB(status="running")
    db.records = db.records[:1]
    sent = _stub_recorded_groups(monkeypatch, module)
    events = []
    errors = []
    monkeypatch.setattr(kill_service, "_runner_process_create_time", lambda pid: ("gone", None))
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(kill_service, "log_error", errors.append)
    monkeypatch.setattr(
        kill_service,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )
    original_settle = db.settle_cli_worker_spawn
    original_get = db.get_session

    async def settle(*args, **kwargs):
        events.append("settle")
        return await original_settle(*args, **kwargs)

    async def get(*args, **kwargs):
        events.append("final-read")
        return await original_get(*args, **kwargs)

    async def resume_during_grace(_delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        events.append("resume")
        db.session["status"] = "running"
        db.session["cli_worker_generation"] = "generation-b"
        db.session["updated_at"] = 43.0
        db.session["node_metadata"] = {"pid": 76544}

    db.settle_cli_worker_spawn = settle
    db.get_session = get
    monkeypatch.setattr(kill_service, "sleep", resume_during_grace)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0.5
    )
    assert result == 1
    assert db.cancelled == [("session-test", "generation-a")]
    assert db.cancel_identities == [{"pid": 76543}]
    assert sent == [(76541, signal.SIGTERM)]
    assert db.guard_calls == [
        ("session-test", "cancelled", "generation-a", db.records[0]["spawn_id"])
    ]
    assert db.settled == [db.records[0]["spawn_id"]]
    assert events == ["resume", "settle", "final-read"]
    assert db.session["status"] == "running"
    assert db.session["cli_worker_generation"] == "generation-b"
    assert db.session["updated_at"] == 43.0
    assert db.session["node_metadata"] == {"pid": 76544}
    assert any("changed during cleanup" in msg for msg in errors)
    output = capsys.readouterr().out
    assert "terminal status remains" not in output
    assert "killed session" not in output


@pytest.mark.asyncio
async def test_terminal_mixed_refusal_and_resume_never_reports_stale_terminal_status(
    monkeypatch, capsys
):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal, _RecordedGroupWitness

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    errors = []
    warnings = []
    events = []

    def admit(identity, **_kwargs):
        if identity.pid == 76541:
            return _RecordedGroupRefusal("identity_mismatch")
        return _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        )

    async def resume_during_grace(_delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        events.append("resume")
        db.session["status"] = "running"
        db.session["cli_worker_generation"] = "generation-b"

    original_settle = db.settle_cli_worker_spawn
    original_get = db.get_session

    async def settle(*args, **kwargs):
        events.append("settle")
        return await original_settle(*args, **kwargs)

    async def get(*args, **kwargs):
        events.append("final-read")
        return await original_get(*args, **kwargs)

    db.settle_cli_worker_spawn = settle
    db.get_session = get
    monkeypatch.setattr(kill_service, "_admit_recorded_worker_group", admit)
    monkeypatch.setattr(
        kill_service,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )
    monkeypatch.setattr(kill_service, "sleep", resume_during_grace)
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(kill_service, "log_error", errors.append)
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)

    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0.5
    )
    assert result == 1
    assert sent == [(76542, signal.SIGTERM)]
    assert db.settled == [db.records[1]["spawn_id"]]
    assert events == ["resume", "settle", "final-read"]
    assert db.session["status"] == "running"
    assert db.session["cli_worker_generation"] == "generation-b"
    assert any("changed during cleanup" in msg for msg in errors)
    assert not any("already in terminal state" in msg for msg in errors)
    assert not any("terminal status remains" in msg for msg in warnings)
    assert "terminal status remains" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_final_session_read_failure_reports_unconfirmed_without_stale_status(
    monkeypatch, capsys
):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB()
    db.records = db.records[:1]
    sent = _stub_recorded_groups(monkeypatch, module)
    warnings = []
    monkeypatch.setattr(module, "warn", warnings.append)
    monkeypatch.setattr(kill_service, "warn", warnings.append)
    monkeypatch.setattr(
        kill_service,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )

    async def fail_read(_sid):
        raise TimeoutError("read failed")

    db.get_session = fail_read
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == [(76541, signal.SIGTERM)]
    assert db.settled == [db.records[0]["spawn_id"]]
    assert any("could not be confirmed stopped" in msg for msg in warnings)
    assert all("terminal status remains" not in msg for msg in warnings)
    assert "terminal status remains" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_terminal_generations_share_grace_and_kill_only_survivor(monkeypatch, capsys):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module

    db = _CleanupDB()
    db.records[0]["generation_id"] = "old-generation"
    db.records[1]["generation_id"] = "generation-a"
    before = dict(db.session)
    _stub_recorded_groups(monkeypatch, module)
    events = []

    def fake_killpg(pgid, sig):
        assert pgid in (76541, 76542)
        events.append(("signal", pgid, sig))

    def matches(witness):
        pgid = witness.identity.pgid
        if pgid == 76542 and ("signal", pgid, signal.SIGTERM) in events:
            return "missing"
        return "matches"

    def group_state(identity):
        pid = identity.pgid
        signal_needed = signal.SIGKILL if pid == 76541 else signal.SIGTERM
        return "empty" if ("signal", pid, signal_needed) in events else "present"

    async def fake_sleep(delay):
        if db.in_signal_guard:
            await _real_sleep(0)
            return
        events.append(("sleep", delay))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(kill_service, "_recorded_worker_group_matches", matches)
    monkeypatch.setattr(kill_service, "_recorded_worker_group_state", group_state)
    monkeypatch.setattr(kill_service, "sleep", fake_sleep)

    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0.5
    )

    assert result == 0
    assert events == [
        ("signal", 76541, signal.SIGTERM),
        ("signal", 76542, signal.SIGTERM),
        ("sleep", 0.5),
        ("signal", 76541, signal.SIGKILL),
    ]
    assert db.settled == [record["spawn_id"] for record in db.records]
    assert db.session == before
    assert db.cancelled == []
    output = capsys.readouterr().out
    assert "pid=76541, pgid=76541, signal=sigkill" in output
    assert "pid=76542, pgid=76542, signal=sigterm" in output


@pytest.mark.asyncio
async def test_terminal_identity_mismatch_never_signals_or_settles_that_record(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal, _RecordedGroupWitness

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    probed = []
    errors = []

    def admit(identity, **_kwargs):
        if identity.pid == 76541:
            return _RecordedGroupRefusal("identity_mismatch")
        return _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        )

    def matches(witness):
        return "missing" if sent else "matches"

    def group_state(identity):
        probed.append(identity.pgid)
        assert identity.pgid == 76542
        return "empty"

    monkeypatch.setattr(kill_service, "_admit_recorded_worker_group", admit)
    monkeypatch.setattr(kill_service, "_recorded_worker_group_matches", matches)
    monkeypatch.setattr(kill_service, "_recorded_worker_group_state", group_state)
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(kill_service, "log_error", errors.append)

    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )

    assert result == 1
    assert sent == [(76542, signal.SIGTERM)]
    assert probed == [76542]
    assert db.settled == [db.records[1]["spawn_id"]]
    assert db.records[0]["spawn_id"] not in db.settled
    assert db.cancelled == []
    assert any("already in terminal state: 'failed'" in msg for msg in errors)


@pytest.mark.asyncio
async def test_terminal_changed_signal_guard_stops_remaining_groups_and_settlement(monkeypatch):
    import lionhub.orchestration.kill as module

    db = _CleanupDB(guards=("unchanged", "changed"))
    sent = _stub_recorded_groups(monkeypatch, module)
    before = dict(db.session)
    result = await module._try_recorded_worker_cleanup(
        db, db.session, user_reason="", grace_seconds=0
    )

    assert result == 1
    assert sent == [(76541, signal.SIGTERM)]
    assert db.settled == []
    assert db.cancelled == []
    assert db.session == before


@pytest.mark.asyncio
async def test_terminal_already_empty_record_and_stopped_worker_both_settle(monkeypatch, capsys):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal, _RecordedGroupWitness

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    errors = []

    def admit(identity, **_kwargs):
        if identity.pid == 76541:
            return _RecordedGroupRefusal("leader_not_live")
        return _RecordedGroupWitness(
            identity,
            (identity.pid, identity.pid_create_time),
            ((identity.pid, identity.pid_create_time),),
        )

    monkeypatch.setattr(kill_service, "_admit_recorded_worker_group", admit)
    monkeypatch.setattr(
        kill_service,
        "_recorded_worker_group_matches",
        lambda witness: "missing" if sent else "matches",
    )
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(kill_service, "log_error", errors.append)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 0
    assert sent == [(76542, signal.SIGTERM)]
    assert db.settled == [record["spawn_id"] for record in db.records]
    assert errors == []
    output = capsys.readouterr().out
    assert output.count("stopped worker for session") == 1
    assert "pid=76542, pgid=76542, signal=sigterm" in output
    assert "pid=76541" not in output


@pytest.mark.asyncio
async def test_terminal_already_empty_records_without_signal_refuse(monkeypatch):
    import lionhub.orchestration.kill as kill_service
    import lionhub.orchestration.kill as module
    from lionagi.ln._proc import _RecordedGroupRefusal

    db = _CleanupDB()
    sent = _stub_recorded_groups(monkeypatch, module)
    errors = []
    monkeypatch.setattr(
        kill_service,
        "_admit_recorded_worker_group",
        lambda identity, **kwargs: _RecordedGroupRefusal("leader_not_live"),
    )
    monkeypatch.setattr(module, "log_error", errors.append)
    monkeypatch.setattr(kill_service, "log_error", errors.append)
    result = await module._try_recorded_worker_cleanup(
        db, dict(db.session), user_reason="", grace_seconds=0
    )
    assert result == 1
    assert sent == []
    assert db.settled == [record["spawn_id"] for record in db.records]
    assert any("already in terminal state: 'failed'" in msg for msg in errors)
