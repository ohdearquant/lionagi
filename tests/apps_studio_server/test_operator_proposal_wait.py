# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Durable proposal decisions use one reader and retain each tool's policy."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from lionagi.studio.operator import _common


def _proposal(status: str, *, expires_at: float = 10.0) -> dict[str, Any]:
    return {"id": "proposal", "status": status, "expiresAt": expires_at}


class _Store:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.events: list[tuple[str, object]] = []
        self.expired = _proposal("expired")
        self.read_error: BaseException | None = None
        self.expiry_error: BaseException | None = None

    async def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        self.events.append(("read", proposal_id))
        if self.read_error is not None:
            raise self.read_error
        assert self.rows, "the decision reader fetched an unexpected proposal"
        return self.rows.pop(0)

    async def expire_proposal(self, proposal_id: str) -> dict[str, Any]:
        self.events.append(("expire", proposal_id))
        if self.expiry_error is not None:
            raise self.expiry_error
        return self.expired


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "expired", "conflict"])
async def test_terminal_decisions_are_refreshed_and_returned_without_waiting(status):
    terminal = _proposal(status)
    store = _Store([terminal])

    def no_clock():
        raise AssertionError("a terminal decision must not check pending expiry")

    async def no_sleep(_interval):
        raise AssertionError("a terminal decision must not wait again")

    result = await _common.wait_for_proposal(
        store, {"id": "proposal"}, clock=no_clock, sleep=no_sleep
    )

    assert result is terminal
    assert store.events == [("read", "proposal")]


@pytest.mark.asyncio
async def test_pending_confirmed_and_executing_decisions_keep_fixed_polling():
    terminal = _proposal("succeeded")
    store = _Store(
        [
            _proposal("pending"),
            _proposal("confirmed", expires_at=0),
            _proposal("executing", expires_at=0),
            terminal,
        ]
    )
    clock_reads = []
    intervals = []

    def clock():
        clock_reads.append(True)
        return 9.0

    async def sleep(interval):
        intervals.append(interval)

    result = await _common.wait_for_proposal(store, {"id": "proposal"}, clock=clock, sleep=sleep)

    assert result is terminal
    assert clock_reads == [True]
    assert intervals == [0.1, 0.1, 0.1]
    assert store.events == [("read", "proposal")] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["expired", "succeeded"])
async def test_pending_expiry_uses_the_store_result_at_the_deadline(outcome):
    store = _Store([_proposal("pending", expires_at=5.0)])
    store.expired = _proposal(outcome)

    async def no_sleep(_interval):
        raise AssertionError("the expiry decision is already terminal")

    result = await _common.wait_for_proposal(
        store, {"id": "proposal"}, clock=lambda: 5.0, sleep=no_sleep
    )

    assert result is store.expired
    assert store.events == [("read", "proposal"), ("expire", "proposal")]


@pytest.mark.asyncio
async def test_a_confirmation_racing_expiry_is_followed_until_it_finishes():
    terminal = _proposal("succeeded")
    store = _Store([_proposal("pending", expires_at=0), terminal])
    store.expired = _proposal("confirmed", expires_at=0)
    intervals = []

    async def sleep(interval):
        intervals.append(interval)

    result = await _common.wait_for_proposal(
        store, {"id": "proposal"}, clock=lambda: 0.0, sleep=sleep
    )

    assert result is terminal
    assert store.events == [
        ("read", "proposal"),
        ("expire", "proposal"),
        ("read", "proposal"),
    ]
    assert intervals == [0.1]


@pytest.mark.asyncio
async def test_the_callers_backoff_policy_keeps_its_ceiling():
    store = _Store([_proposal("pending")] * 8 + [_proposal("succeeded")])
    intervals = []

    async def sleep(interval):
        intervals.append(interval)

    await _common.wait_for_proposal(
        store,
        {"id": "proposal"},
        clock=lambda: 0.0,
        sleep=sleep,
        poll_interval=0.1,
        advance_interval=lambda interval: min(interval * 2, 5.0),
    )

    assert intervals == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["read", "expiry", "sleep"])
@pytest.mark.parametrize("cancelled", [False, True], ids=["error", "cancelled"])
async def test_decision_errors_and_cancellation_propagate_without_another_read(stage, cancelled):
    failure = asyncio.CancelledError() if cancelled else ValueError("decision unavailable")
    store = _Store([_proposal("pending", expires_at=0 if stage == "expiry" else 1)])
    if stage == "read":
        store.read_error = failure
    elif stage == "expiry":
        store.expiry_error = failure

    async def sleep(_interval):
        raise failure

    with pytest.raises(type(failure)) as caught:
        await _common.wait_for_proposal(store, {"id": "proposal"}, clock=lambda: 0.0, sleep=sleep)

    assert caught.value is failure
    expected = [("read", "proposal")]
    if stage == "expiry":
        expected.append(("expire", "proposal"))
    assert store.events == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name",
    ["application_mcp", "cancel_run", "rename_session", "resume_run", "run_control"],
    ids=["launch", "cancel", "rename", "resume", "control"],
)
async def test_proposal_tools_use_the_shared_decision_reader(monkeypatch, module_name):
    module = importlib.import_module(f"lionagi.studio.operator.{module_name}")
    initial = {"id": "proposal"}
    terminal = {"id": "proposal", "status": "failed", "result": {"run_id": "child"}}
    row = {
        "id": "session",
        "status": "running",
        "invocation_kind": "agent" if module_name == "resume_run" else "flow",
        "name": "Current name",
        "project": "sample",
    }

    class ToolStore:
        @staticmethod
        def canonical_hash(_value):
            return "stable"

        async def create_proposal(self, *_args, **_kwargs):
            return initial

        async def get_proposal(self, _proposal_id):
            return {"id": "proposal", "status": "cancelled"}

    store = ToolStore()
    monkeypatch.setattr(module, "_identity", lambda: (store, "conversation", "request"))

    async def resolve(*_args, **_kwargs):
        return {"found": True, "ambiguous": False, "session_id": "session", "run": row}

    if module_name == "application_mcp":

        async def version(_playbook):
            return "version"

        async def no_problem(*_args, **_kwargs):
            return None

        monkeypatch.setattr(module, "resolve_playbook_version", version)
        monkeypatch.setattr(module, "run_sync", no_problem)
    elif module_name == "cancel_run":
        monkeypatch.setattr(module, "_resolve_reference", resolve)
    else:
        monkeypatch.setattr(module, "resolve_run", resolve)

    if module_name == "resume_run":
        monkeypatch.setattr(module, "_kind_argument_mismatch", lambda *_args: None)

    if module_name in {"rename_session", "resume_run"}:
        state_db = importlib.import_module("lionagi.state.db")

        class ReadStore:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def fetch_one(self, *_args):
                return row

            async def get_session(self, _session_id):
                return row

            @staticmethod
            def _row_to_dict(value):
                return dict(value)

        monkeypatch.setattr(state_db, "StateDB", ReadStore)
        monkeypatch.setattr(state_db, "read_only_open_supported", lambda: False)
    elif module_name == "run_control":

        async def project(*_args):
            return "sample"

        async def load(_session_id):
            return row

        monkeypatch.setattr(module, "_allowed_project", project)
        monkeypatch.setattr(module, "_load_run", load)
        monkeypatch.setattr(module, "session_has_control_consumer", lambda _session: True)
        monkeypatch.setattr(module, "_MIN_PROPOSAL_POLL_SECONDS", 0.125)

    delegated = []
    delays = []

    async def shared_reader(actual_store, proposal, **policy):
        delegated.append((actual_store, proposal))
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 123.0))

        async def changed_sleep(interval):
            delays.append(interval)

        monkeypatch.setattr(module, "sleep", changed_sleep)
        assert policy["clock"]() == 123.0
        await policy["sleep"](0.25)
        if module_name == "run_control":
            assert policy["poll_interval"] == 0.125
            monkeypatch.setattr(module, "_MAX_PROPOSAL_POLL_SECONDS", 0.3)
            assert policy["advance_interval"](0.2) == 0.3
            monkeypatch.setattr(module, "_TERMINAL_PROPOSAL_STATUSES", frozenset({"custom"}))
            assert policy["is_terminal"]("custom")
            assert not policy["is_terminal"]("succeeded")
        else:
            assert policy.get("poll_interval", 0.1) == 0.1
            assert policy.get("advance_interval") is None
        return terminal

    monkeypatch.setattr(_common, "wait_for_proposal", shared_reader)
    if module_name == "application_mcp":
        result = await module.launch_playbook({"playbook": "sample"})
        expected = {
            "status": "failed",
            "proposalId": "proposal",
            "result": {"run_id": "child"},
            "errorCode": None,
        }
    elif module_name == "run_control":
        result = await module._propose_run_control(run="session", verb="pause", payload=None)
        expected = {"queued": False, "reason": "failed", "id": "session"}
    else:
        arguments = {"run": "session"}
        if module_name == "rename_session":
            arguments["name"] = "Renamed"
        elif module_name == "resume_run":
            arguments["instruction"] = "Continue"
        result = await getattr(module, module_name)(arguments)
        key = {"cancel_run": "cancelled", "rename_session": "renamed", "resume_run": "resumed"}[
            module_name
        ]
        expected = {key: False, "reason": "failed", "id": "session"}
        if module_name == "cancel_run":
            expected["run_untouched"] = True

    assert delegated, "proposal tools must use the shared decision reader"
    assert delegated == [(store, initial)]
    assert delays == [0.25]
    assert result == expected
