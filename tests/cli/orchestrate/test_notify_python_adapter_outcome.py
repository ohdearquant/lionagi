# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Whether a terminal notification went out must not depend on adapter type.

The exec adapter records every failure mode it has -- timeout, spawn failure,
nonzero exit -- onto the run, so a caller can ask afterwards whether delivery
succeeded. A python adapter is handed back as the imported callable with no
outcome recording wrapped around it, so when it raised, the only trace was a log
line. A caller cannot reasonably be expected to know that the answer is
queryable for one spec shape and not the other.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from lionagi.cli._runs import RunDir
from lionagi.cli.orchestrate._notify import deliver_flow_notify_now

_ADAPTER_MODULE = "_lionagi_test_notify_adapter"


def _install_adapter(monkeypatch: pytest.MonkeyPatch, fn) -> dict:
    """Register an importable python adapter and return its resolver mapping."""
    mod = types.ModuleType(_ADAPTER_MODULE)
    mod.adapter = fn
    monkeypatch.setitem(sys.modules, _ADAPTER_MODULE, mod)
    # The mapping shape is what selects a python adapter. A bare string is
    # always an exec command, so passing one here would exercise the exec path
    # and pass for a reason that has nothing to do with this behaviour.
    return {"adapter": {"kind": "python", "ref": f"{_ADAPTER_MODULE}:adapter"}}


def _run(tmp_path: Path) -> RunDir:
    state_root = tmp_path / "state"
    artifact_root = state_root / "artifacts"
    artifact_root.mkdir(parents=True)
    return RunDir(run_id="r-1", state_root=state_root, artifact_root=artifact_root)


async def _deliver(run: RunDir, override: dict) -> None:
    await deliver_flow_notify_now(
        override=override,
        run=run,
        entity_kind="session",
        entity_id="sess-1",
        invocation_id=None,
        flow_kind="flow",
        playbook=None,
        save_dir=None,
        cwd=str(run.state_root),
        started_at=0.0,
        terminal_status="completed",
        reason_code="",
        occurred_at=0.0,
    )


async def test_a_python_adapter_that_raises_leaves_a_durable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A raise must be queryable afterwards, not only present in a log.

    This is the arm that separates recording the failure from swallowing it: the
    delivery already caught the exception and carried on, so nothing about the
    run's success or its exit status changes either way. The only observable
    difference is whether the outcome file exists.
    """

    def _boom(_envelope):
        raise RuntimeError("adapter exploded")

    run = _run(tmp_path)
    await _deliver(run, _install_adapter(monkeypatch, _boom))

    assert run.notify_outcome_path.exists(), (
        "a python adapter raised and left nothing to query; "
        "the same failure through an exec adapter is recorded"
    )
    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome["ok"] is False, outcome


async def test_a_python_adapter_that_returns_is_not_recorded_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The success arm, without which the test above passes on a handler that
    records failure unconditionally."""
    delivered: list[object] = []

    def _ok(envelope):
        delivered.append(envelope)

    run = _run(tmp_path)
    await _deliver(run, _install_adapter(monkeypatch, _ok))

    assert delivered, "the adapter was never called, so neither arm means anything"
    if run.notify_outcome_path.exists():
        outcome = json.loads(run.notify_outcome_path.read_text())
        assert outcome["ok"] is not False, (
            f"a successful python adapter was recorded as a failure: {outcome}"
        )


async def test_the_exception_text_is_not_placed_in_the_outcome_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Adapter output is free text that can carry a credential from any source,
    so it is referenced by path and kept out of the record itself -- the same
    rule the exec adapter's stderr already follows."""

    def _boom(_envelope):
        raise RuntimeError("token=sk-not-a-real-secret-abcdef")

    run = _run(tmp_path)
    await _deliver(run, _install_adapter(monkeypatch, _boom))

    assert run.notify_outcome_path.exists()
    raw = run.notify_outcome_path.read_text()
    assert "sk-not-a-real-secret-abcdef" not in raw, (
        f"exception text was written into the outcome record: {raw}"
    )


@pytest.mark.parametrize("adapter_kind", ["sync", "async"])
async def test_python_adapter_error_only_exposes_type_in_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, adapter_kind
):
    secret = "PRIVATE_ADAPTER_EXCEPTION_SENTINEL"

    def forbid(*_args, **_kwargs):
        pytest.fail("test must not invoke processes, signals, or network")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)

    def fail(_envelope):
        raise RuntimeError(secret)

    async def async_fail(envelope):
        fail(envelope)

    run = _run(tmp_path)
    adapter = fail if adapter_kind == "sync" else async_fail
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.orchestrate._notify"):
        await _deliver(run, _install_adapter(monkeypatch, adapter))

    records = [
        record
        for record in caplog.records
        if "direct-path notify.on_terminal delivery failed" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].getMessage().endswith(f"for run {run.run_id} (RuntimeError)")
    assert records[0].exc_info is None and records[0].exc_text is None
    assert secret not in caplog.text and secret not in repr(records[0].args)
    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome["ok"] is False and outcome["stderr_path"] == str(run.notify_stderr_path)
    assert secret not in run.notify_outcome_path.read_text()
    traceback_text = run.notify_stderr_path.read_text()
    assert "Traceback (most recent call last)" in traceback_text
    assert f"RuntimeError: {secret}" in traceback_text
    if os.name == "posix":
        assert stat.S_IMODE(run.notify_stderr_path.stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize("adapter_kind", ["sync", "async"])
@pytest.mark.parametrize("write_failure", [None, "stderr", "outcome"])
async def test_debug_logs_do_not_chain_adapter_failure_on_recording_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    adapter_kind: str,
    write_failure: str | None,
):
    secret = "PRIVATE_ADAPTER_EXCEPTION_SENTINEL"

    def forbid(*_args, **_kwargs):
        pytest.fail("test must not invoke processes, signals, or network")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)

    def fail(_envelope):
        raise RuntimeError(secret)

    async def async_fail(envelope):
        fail(envelope)

    def fail_write(_run, *_args, **_kwargs):
        raise OSError(f"disk full: {secret}")

    if write_failure == "stderr":
        monkeypatch.setattr(RunDir, "write_notify_stderr", fail_write)
    elif write_failure == "outcome":
        monkeypatch.setattr(RunDir, "write_notify_outcome", fail_write)

    run = _run(tmp_path)
    adapter = fail if adapter_kind == "sync" else async_fail
    with caplog.at_level(logging.DEBUG, logger="lionagi"):
        await _deliver(run, _install_adapter(monkeypatch, adapter))

    formatter = logging.Formatter()
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in repr(record.args)
        assert secret not in (record.stack_info or "")
        if record.exc_info:
            assert secret not in formatter.formatException(record.exc_info)
    assert secret not in caplog.text

    if write_failure is None:
        outcome = json.loads(run.notify_outcome_path.read_text())
        assert outcome["ok"] is False
        assert outcome["stderr_path"] == str(run.notify_stderr_path)
        assert f"RuntimeError: {secret}" in run.notify_stderr_path.read_text()
        assert secret not in run.notify_outcome_path.read_text()
    elif write_failure == "stderr":
        outcome = json.loads(run.notify_outcome_path.read_text())
        assert outcome["ok"] is False and outcome["stderr_path"] is None
        assert not run.notify_stderr_path.exists()
        assert any(
            "failed to capture notify.on_terminal adapter stderr" in r.getMessage()
            for r in caplog.records
        )
    else:
        assert not run.notify_outcome_path.exists()
        assert f"RuntimeError: {secret}" in run.notify_stderr_path.read_text()
        assert any(
            "failed to record notify.on_terminal outcome" in r.getMessage() for r in caplog.records
        )
