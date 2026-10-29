# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Offline controls for durable settings, scope diagnostics and exec failures."""

import asyncio
import inspect
import os
import socket
import subprocess
from pathlib import Path

import pytest

from lionagi import _settings
from lionagi.state.lifecycle import notify_settings, settings_delivery
from lionagi.state.lifecycle.reconciler import DeliveryOutcome
from lionagi.studio.services.terminal_reconciliation import prepare_global_settings_delivery


def _forbid(*_args, **_kwargs):
    raise AssertionError("fake-only test forbids subprocesses, signals and network")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


def row():
    return {
        "transition_id": "event-1",
        "entity_type": "session",
        "entity_id": "session-1",
        "previous_status": "running",
        "terminal_status": "failed",
        "reason_code": "safe",
        "occurred_at": 1.0,
    }


class Recorder:
    def __init__(self):
        self.records = []

    def record(self, scope, **kwargs):
        self.records.append((scope, kwargs))
        return None


@pytest.mark.parametrize("which", ["file", "directory"])
def test_broken_global_symlink_is_read_failure_and_missing_is_empty(tmp_path, monkeypatch, which):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _settings.load_global_settings_layer() == {}
    directory = tmp_path / ".lionagi"
    if which == "directory":
        directory.symlink_to(tmp_path / "missing-directory", target_is_directory=True)
    else:
        directory.mkdir()
        (directory / "settings.yaml").symlink_to(tmp_path / "missing-settings")
    with pytest.raises(FileNotFoundError):
        _settings.load_global_settings_layer()


@pytest.mark.parametrize("which", ["file", "directory"])
def test_broken_nearest_project_symlink_refuses_fallback(tmp_path, which):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".lionagi").mkdir()
    (root / ".lionagi" / "settings.yaml").write_text("notify: {}\n")
    child = root / "child"
    child.mkdir()
    assert _settings.load_project_settings_layer(child) == {"notify": {}}
    directory = child / ".lionagi"
    if which == "directory":
        directory.symlink_to(child / "missing-directory", target_is_directory=True)
    else:
        directory.mkdir()
        (directory / "settings.yaml").symlink_to(child / "missing-settings")
    with pytest.raises(FileNotFoundError):
        _settings.load_project_settings_layer(child)


@pytest.mark.parametrize(
    ("scope", "global_settings", "project_settings", "reason"),
    [
        ("global", None, {}, "settings_root_not_mapping"),
        ("global", {"notify": "SECRET_VALUE"}, {}, "notify_not_mapping"),
        (
            "global",
            {"notify": {"on_terminal": {"enabled": "SECRET_VALUE"}}},
            {},
            "enabled_not_boolean",
        ),
        ("global", {"notify": {"on_terminal": {"enabled": True}}}, {}, "enabled_without_adapter"),
        ("project", {}, {"notify": "SECRET_VALUE"}, "notify_not_mapping"),
        (
            "project",
            {},
            {"notify": {"on_terminal": {"adapter": {"kind": "SECRET_VALUE"}}}},
            "adapter_kind_unsupported",
        ),
    ],
)
def test_rejections_log_correct_scope_and_stable_reason_only(
    caplog, scope, global_settings, project_settings, reason
):
    with caplog.at_level("WARNING"):
        notify_settings.classify_notify_provenance(global_settings, project_settings)
    messages = [record.getMessage() for record in caplog.records]
    matching = [m for m in messages if f"{scope} configuration rejected: {reason}" in m]
    assert len(matching) == 1
    assert "SECRET_VALUE" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("launch", "exec_adapter_launch_failed"),
        ("post_spawn", "exec_adapter_post_spawn_failed"),
        ("pre_try", "exec_adapter_execution_failed"),
        ("success", None),
    ],
)
async def test_exec_outcome_records_once_and_post_spawn_is_cleaned(
    monkeypatch, caplog, mode, reason
):
    recorder, calls = Recorder(), []
    secret = "PRIVATE_FAILURE_SENTINEL"
    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "exec", "argv": ["fake"]}}}},
    )

    class FakeProcess:
        pid = 4242
        returncode = 0

        async def communicate(self, _payload):
            if mode == "post_spawn":
                raise OSError(secret)
            return b"", b""

    proc = FakeProcess()

    async def spawn(*_args, **_kwargs):
        calls.append("spawn")
        if mode == "launch":
            raise OSError(secret)
        return proc

    async def terminate(child, **_kwargs):
        assert child is proc
        calls.append("terminate")

    async def reap(child, **_kwargs):
        assert child is proc
        calls.append("reap")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(notify_settings, "observe_process_group_leader", lambda *_a, **_k: 1.0)
    monkeypatch.setattr(
        notify_settings, "capture_group_witnesses", lambda *_a, **_k: ((4242, 1.0),)
    )
    monkeypatch.setattr(notify_settings, "aterminate_process_group", terminate)
    monkeypatch.setattr(notify_settings, "_await_proc_dead", reap)
    if mode == "pre_try":

        def fail_env():
            raise OSError(secret)

        monkeypatch.setattr(notify_settings, "env_without_state_db_url", fail_env)
    prepared = await prepare_global_settings_delivery(row(), None, recorder=recorder)
    with caplog.at_level("WARNING"):
        pending = prepared.call()
        assert inspect.isawaitable(pending)
        result = await pending
    assert result is (DeliveryOutcome.SUCCESS if mode == "success" else DeliveryOutcome.FAILURE)
    assert len(recorder.records) == 1
    scope, outcome = recorder.records[0]
    assert (
        scope == "global" and outcome["reason"] == reason and outcome["ok"] is (mode == "success")
    )
    assert (
        calls
        == {
            "launch": ["spawn"],
            "post_spawn": ["spawn", "terminate", "reap"],
            "pre_try": [],
            "success": ["spawn"],
        }[mode]
    )
    assert secret not in caplog.text and secret not in repr(recorder.records)
    if reason:
        assert reason in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("dangling", [False, True], ids=["missing", "dangling"])
async def test_global_missing_is_inapplicable_but_dangling_stays_owed(
    tmp_path, monkeypatch, dangling
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if dangling:
        directory = tmp_path / ".lionagi"
        directory.mkdir()
        (directory / "settings.yaml").symlink_to(tmp_path / "missing-settings")
    recorder = Recorder()
    prepared = await prepare_global_settings_delivery(row(), None, recorder=recorder)
    result = prepared.call()
    assert result is (DeliveryOutcome.FAILURE if dangling else DeliveryOutcome.NOT_APPLICABLE)
    if dangling:
        assert len(recorder.records) == 1
        assert recorder.records[0][1]["reason"] == "settings_load_failed"
    else:
        assert recorder.records == []


@pytest.mark.asyncio
async def test_concurrent_calls_of_same_prepared_delivery_keep_outcomes_separate(monkeypatch):
    recorder = Recorder()
    first_recorded = asyncio.Event()
    release_first = asyncio.Event()
    attempts = 0

    monkeypatch.setattr(
        settings_delivery,
        "load_global_settings_layer",
        lambda: {"notify": {"on_terminal": {"adapter": {"kind": "exec", "argv": ["fake"]}}}},
    )

    def fake_build_handler(_resolved, *, outcome_fn, **_kwargs):
        async def handler(_envelope):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                outcome_fn(ok=True, exit_code=0, stderr_text=None)
                first_recorded.set()
                await release_first.wait()
            else:
                outcome_fn(ok=False, exit_code=1, stderr_text=None)

        return handler

    monkeypatch.setattr(settings_delivery, "build_handler", fake_build_handler)
    prepared = await prepare_global_settings_delivery(row(), None, recorder=recorder)
    first = asyncio.create_task(prepared.call())
    second = None
    try:
        await asyncio.wait_for(first_recorded.wait(), 1)
        second = asyncio.create_task(prepared.call())
        assert await asyncio.wait_for(second, 1) is DeliveryOutcome.FAILURE
        release_first.set()
        assert await asyncio.wait_for(first, 1) is DeliveryOutcome.SUCCESS
        assert [data["ok"] for scope, data in recorder.records] == [True, False]
        assert [scope for scope, _data in recorder.records] == ["global", "global"]
    finally:
        release_first.set()
        await asyncio.gather(first, *(task for task in (second,) if task), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_notify", ["PRIVATE_VALUE", ["PRIVATE_VALUE"], 7])
async def test_non_mapping_global_notify_remains_owed(monkeypatch, caplog, invalid_notify):
    monkeypatch.setattr(
        settings_delivery, "load_global_settings_layer", lambda: {"notify": invalid_notify}
    )
    recorder = Recorder()
    prepared = await prepare_global_settings_delivery(row(), None, recorder=recorder)
    with caplog.at_level("WARNING"):
        result = prepared.call()
    assert result is DeliveryOutcome.FAILURE
    assert len(recorder.records) == 1
    scope, outcome = recorder.records[0]
    assert scope == "global"
    assert outcome["ok"] is False and outcome["reason"] == "notify_not_mapping"
    assert "notify_not_mapping" in caplog.text
    assert "PRIVATE_VALUE" not in caplog.text
    assert "PRIVATE_VALUE" not in repr(recorder.records)
