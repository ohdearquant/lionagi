# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Agent execution owns its admitted worker generation through teardown."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio


def _identity():
    return {
        "pid": 12345,
        "pgid": 12345,
        "pid_create_time": 1000.25,
        "pid_host": "synthetic-host",
        "pid_boot_time": 10.5,
        "process_identity_mode": "local",
    }


async def _record(owner):
    admission = owner.admit()
    try:
        await admission.record(_identity())
    finally:
        admission.finish()


async def _join(task):
    return await asyncio.wait_for(task, 5)


@pytest_asyncio.fixture(loop_scope="function")
async def store(monkeypatch, tmp_path):
    from lionagi.state import db as module

    monkeypatch.setattr(module, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LIONAGI_RUN_ID", raising=False)
    yield module
    await module.close_shared_db()


@pytest.mark.asyncio(loop_scope="function")
async def test_fresh_generation_is_admitted_before_setup_hook_work(store, monkeypatch):
    from lionagi import Branch
    from lionagi._cli_worker_context import _get_cli_worker_recorder
    from lionagi.cli import _runs
    from lionagi.hooks.bus import HookBus, HookPoint

    entered, release = asyncio.Event(), asyncio.Event()
    original_record = store.StateDB.register_cli_worker_spawn
    original_emit = HookBus.emit
    observed = {}

    async def held_record(db, record):
        observed["payload"] = copy.deepcopy(record)
        entered.set()
        await release.wait()
        await original_record(db, record)

    async def emit(bus, point, **payload):
        if point == HookPoint.SESSION_START:
            owner = _get_cli_worker_recorder()
            assert owner is not None, "setup hooks must inherit the admitted worker owner"
            observed["owner"] = owner
            await _record(owner)
        return await original_emit(bus, point, **payload)

    monkeypatch.setattr(store.StateDB, "register_cli_worker_spawn", held_record)
    monkeypatch.setattr(HookBus, "emit", emit)
    branch = Branch()
    task = asyncio.create_task(_runs.setup_agent_persist(branch, share_db=False, run_id="fresh"))
    ctx = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not task.done(), "setup must await registration started by its hook"
        async with store.StateDB() as check:
            snapshot = await check.snapshot_cli_worker_spawns(observed["owner"].session_id)
        assert snapshot["session"]["status"] == "running"
        assert snapshot["session"]["cli_worker_generation"] == observed["owner"].generation_id
        assert snapshot["spawns"] == []
        release.set()
        ctx = await _join(task)
        assert ctx["worker_spawn_owner"] is observed["owner"]
        assert ctx["worker_spawn_db_closed"] is None
        saved = await ctx["db"].snapshot_cli_worker_spawns(ctx["session_id"])
        assert len(saved["spawns"]) == 1
        assert saved["spawns"][0]["generation_id"] == observed["owner"].generation_id
        assert saved["spawns"][0]["run_id"] == "fresh"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if ctx is not None:
            await _runs.teardown_agent_persist(ctx)
        await branch.mdls.shutdown()


@pytest.mark.asyncio(loop_scope="function")
async def test_terminal_resume_uses_the_adopted_key_and_new_atomic_generation(store):
    from lionagi import Branch
    from lionagi.cli import _runs

    branch = Branch()
    first = await _runs.setup_agent_persist(branch, share_db=False, run_id="parent")
    old = await first["db"].get_session(first["session_id"])
    await _runs.teardown_agent_persist(first)
    second = await _runs.setup_agent_persist(branch, share_db=False, run_id="resumed")
    try:
        owner = second["worker_spawn_owner"]
        row = await second["db"].get_session(second["session_id"])
        assert second["session_id"] == first["session_id"] != str(second["session"].id)
        assert owner.session_id == second["session_id"]
        assert row["status"] == "running"
        assert row["cli_worker_generation"] == owner.generation_id != old["cli_worker_generation"]
        await _record(owner)
        snapshot = await second["db"].snapshot_cli_worker_spawns(owner.session_id)
        assert snapshot["spawns"][0]["run_id"] == "resumed"
        assert snapshot["spawns"][0]["generation_id"] == row["cli_worker_generation"]
    finally:
        await _runs.teardown_agent_persist(second)
        await branch.mdls.shutdown()


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("same_object", [False, True])
async def test_independent_running_resume_preserves_the_first_bound_handle(store, same_object):
    from lionagi import Branch
    from lionagi.cli import _runs
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    branch = Branch()
    first = await _runs.setup_agent_persist(branch, run_id="first")
    before = await first["db"].get_session(first["session_id"])
    clone = branch if same_object else Branch.from_dict(branch.to_dict())
    try:
        with pytest.raises(_WorkerSpawnAdmissionError):
            await _runs.setup_agent_persist(clone, run_id="independent")
        assert await first["db"].get_session(first["session_id"]) == before, (
            "a refused independent resume must leave the first handle usable and row unchanged"
        )
        assert first["worker_spawn_owner"].generation_id == before["cli_worker_generation"]
    finally:
        await _runs.teardown_agent_persist(first)
        await branch.mdls.shutdown()
        if clone is not branch:
            await clone.mdls.shutdown()


@pytest.mark.asyncio(loop_scope="function")
async def test_fresh_session_key_collision_cannot_borrow_an_existing_owner(store, monkeypatch):
    from lionagi import Branch
    from lionagi.cli import _runs
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
    from lionagi.session import session as session_module

    branch = Branch()
    first = await _runs.setup_agent_persist(branch, run_id="first")
    before = await first["db"].get_session(first["session_id"])
    real_session = session_module.Session

    class CollidingSession(real_session):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "id": first["session_id"]})

    other = Branch()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(session_module, "Session", CollidingSession)
            with pytest.raises(_WorkerSpawnAdmissionError):
                await _runs.setup_agent_persist(other, run_id="collision")
        assert await first["db"].get_session(first["session_id"]) == before
        assert await first["db"].get_branch(str(other.id)) is None
    finally:
        await _runs.teardown_agent_persist(first)
        await branch.mdls.shutdown()
        await other.mdls.shutdown()


@pytest.mark.asyncio(loop_scope="function")
async def test_bound_close_failure_cannot_be_a_continuation_witness(store, monkeypatch):
    from lionagi import Branch
    from lionagi.cli import _runs
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
    from lionagi.state.cli_workers import _prepare_worker_spawn_continuation

    branch = Branch()
    ctx = await _runs.setup_agent_persist(branch, share_db=False)
    original_close = ctx["db"].close

    async def failed_close():
        raise OSError("synthetic close failure")

    monkeypatch.setattr(ctx["db"], "close", failed_close)
    try:
        await _runs.teardown_agent_persist(ctx, status="timed_out", defer_terminal=True)
        assert ctx["worker_spawn_db_closed"] is None
        with pytest.raises(_WorkerSpawnAdmissionError):
            _prepare_worker_spawn_continuation(
                ctx["worker_spawn_owner"], db_close_witness=ctx["worker_spawn_db_closed"]
            )
    finally:
        monkeypatch.setattr(ctx["db"], "close", original_close)
        await original_close()
        await branch.mdls.shutdown()


@pytest.mark.asyncio(loop_scope="function")
async def test_unfinished_drain_error_blocks_terminal_work_and_bound_close(store, monkeypatch):
    from lionagi import Branch
    from lionagi.cli import _runs

    branch = Branch()
    ctx = await _runs.setup_agent_persist(branch, share_db=False)
    owner = ctx["worker_spawn_owner"]
    original = owner.aclose
    error = RuntimeError("synthetic drain failure")

    async def failed_drain():
        raise error

    monkeypatch.setattr(owner, "aclose", failed_drain)
    try:
        with pytest.raises(RuntimeError) as raised:
            await _runs.teardown_agent_persist(ctx)
        assert raised.value is error
        assert ctx["worker_spawn_db_closed"] is None
        assert (await ctx["db"].get_session(ctx["session_id"]))["status"] == "running"
    finally:
        monkeypatch.setattr(owner, "aclose", original)
        await _runs.teardown_agent_persist(ctx)
        await branch.mdls.shutdown()


@pytest.mark.asyncio(loop_scope="function")
async def test_new_agent_setup_masks_an_outer_recorder(monkeypatch, tmp_path):
    from lionagi._cli_worker_context import (
        _bind_cli_worker_recorder,
        _get_cli_worker_recorder,
    )
    from lionagi.cli import agent

    class ReachedSetup(Exception):
        pass

    outer = object()

    def stop_at_setup(*args, **kwargs):
        assert _get_cli_worker_recorder() is None, "new CLI setup must mask inherited ownership"
        raise ReachedSetup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent, "resolve_spawn_mcp_servers", stop_at_setup)
    with _bind_cli_worker_recorder(outer):
        with pytest.raises(ReachedSetup):
            await agent._run_agent("claude_code/sonnet", "hello")
        assert _get_cli_worker_recorder() is outer


@pytest.mark.asyncio(loop_scope="function")
async def test_auto_resumed_flag_without_capability_refuses_before_setup(monkeypatch):
    from lionagi.cli import agent
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    reached = []
    monkeypatch.setattr(
        agent, "_apply_detached_execution_boundary", lambda value: reached.append(value)
    )
    with pytest.raises(_WorkerSpawnAdmissionError):
        await agent._run_agent("claude_code/sonnet", "hello", _auto_resumed=True)
    assert reached == [], "a flag cannot replace the continuation before invocation setup"


@pytest_asyncio.fixture(loop_scope="function")
async def agent_run(store, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from lionagi import Branch
    from lionagi._cli_worker_context import _get_cli_worker_recorder
    from lionagi.cli import agent
    from lionagi.protocols.messages.manager import MessageManager
    from lionagi.service.manager import iModelManager

    state = SimpleNamespace(
        module=agent,
        contexts=[],
        manifests=[],
        runs=[],
        operands=[],
        on_setup=None,
        operate=None,
        snapshot=None,
    )
    original_setup = agent.setup_agent_persist

    async def setup(*args, **kwargs):
        assert _get_cli_worker_recorder() is None
        ctx = await original_setup(*args, **kwargs)
        assert ctx is not None
        state.contexts.append(ctx)
        if state.on_setup is not None:
            await state.on_setup(ctx)
        return ctx

    async def operate(branch, instruction=None, **kwargs):
        ctx = state.contexts[-1]
        owner = _get_cli_worker_recorder()
        assert owner is ctx["worker_spawn_owner"], "caller work must inherit its exact setup owner"
        state.operands.append(owner)
        path = state.runs[-1].branches_dir / f"{branch.id}.json"
        path.write_text(json.dumps(branch.to_dict()))
        state.snapshot = path
        value = await state.operate(owner, len(state.operands), ctx)
        msg = MessageManager.create_assistant_response(assistant_response=value)
        await ctx["hook"](msg)
        return value

    def allocate():
        root = tmp_path / f"run-{len(state.runs)}"
        for name in ["artifacts", "stream", "branches"]:
            (root / name).mkdir(parents=True, exist_ok=True)
        run = SimpleNamespace(
            run_id=f"diagnostic-{len(state.runs)}",
            state_root=root,
            artifact_root=root / "artifacts",
            stream_dir=root / "stream",
            branches_dir=root / "branches",
            write_manifest=lambda value: state.manifests.append(json.dumps(value)),
        )
        state.runs.append(run)
        return run

    monkeypatch.setattr(agent, "setup_agent_persist", setup)
    monkeypatch.setattr(Branch, "operate", operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent, "resolve_persisted_effort", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "build_chat_model", lambda *args, **kwargs: "claude_code/sonnet")
    monkeypatch.setattr(agent, "save_last_branch_pointer", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "resolve_artifact_contract", lambda **kwargs: None)
    monkeypatch.setattr(agent, "allocate_run", allocate)
    monkeypatch.setattr(agent, "find_branch", lambda branch_id: ("parent", state.snapshot))
    yield state


@pytest.mark.asyncio(loop_scope="function")
async def test_real_continuation_waits_for_drain_and_bound_close_then_keeps_generation(
    agent_run,
    store,
    monkeypatch,
):
    from lionagi.state.cli_workers import _WorkerSpawnOwner
    from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS

    state = agent_run
    drain_started, release_drain = asyncio.Event(), asyncio.Event()
    close_started, release_close = asyncio.Event(), asyncio.Event()
    original_drain, original_close = _WorkerSpawnOwner.aclose, store.StateDB.close
    terminal = []
    key = f"worker-owner-{uuid4()}"
    DEFAULT_TERMINAL_CALLBACKS.register(
        key, lambda event: terminal.append(event), kinds=["session"]
    )

    async def operate(owner, turn, ctx):
        await _record(owner)
        if turn == 1:
            state.parent_record = copy.deepcopy(
                (await ctx["db"].snapshot_cli_worker_spawns(owner.session_id))["spawns"][0]
            )
            raise TimeoutError("synthetic timeout")
        return "concluded"

    first_drain = first_close = True

    async def drain(owner):
        nonlocal first_drain
        if state.contexts and owner is state.contexts[0]["worker_spawn_owner"] and first_drain:
            first_drain = False
            drain_started.set()
            await release_drain.wait()
        await original_drain(owner)

    async def close(db):
        nonlocal first_close
        if state.contexts and db is state.contexts[0]["db"] and first_close:
            first_close = False
            close_started.set()
            await release_close.wait()
        await original_close(db)

    state.operate = operate
    monkeypatch.setattr(_WorkerSpawnOwner, "aclose", drain)
    monkeypatch.setattr(store.StateDB, "close", close)
    task = asyncio.create_task(
        state.module._run_agent(
            "claude_code/sonnet",
            "hello",
            timeout=30,
            resume_on_timeout=True,
            no_mcp_config=True,
        )
    )
    try:
        await asyncio.wait_for(drain_started.wait(), 5)
        assert len(state.contexts) == 1 and not task.done()
        release_drain.set()
        await asyncio.wait_for(close_started.wait(), 5)
        assert len(state.contexts) == 1 and state.contexts[0]["worker_spawn_db_closed"] is None
        release_close.set()
        result = await _join(task)
        assert result[0] == "concluded" and result[3] == "completed"
        assert len(state.contexts) == 2
        parent, child = state.contexts
        assert parent["worker_spawn_db_closed"] is parent["db"]
        assert child["session_id"] == parent["session_id"]
        assert (
            child["worker_spawn_owner"].generation_id == parent["worker_spawn_owner"].generation_id
        )
        async with store.StateDB() as check:
            snapshot = await check.snapshot_cli_worker_spawns(child["session_id"])
        assert state.parent_record in snapshot["spawns"]
        assert {row["run_id"] for row in snapshot["spawns"]} == {run.run_id for run in state.runs}
        assert all(row["confirmed_empty_at"] is None for row in snapshot["spawns"])
        assert len(terminal) == 1 and terminal[0].terminal_status == "completed"
        assert all(isinstance(json.loads(value), dict) for value in state.manifests)
    finally:
        release_drain.set()
        release_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        DEFAULT_TERMINAL_CALLBACKS.unregister(key)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("refusal_kind", [None, "store", "compatibility"])
async def test_cancel_during_real_admitted_drain_preserves_the_durable_result(
    agent_run,
    store,
    monkeypatch,
    refusal_kind,
):
    from sqlalchemy.exc import OperationalError

    from lionagi.state.compat import StateCompatibilityRefused

    state = agent_run
    drain_started, cancelled_in_wait, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retained = []
    closed = []
    prepared = []
    observed_cancel = []
    original_close = store.StateDB.close
    original_status = store.StateDB.update_status
    refusal = (
        OperationalError("synthetic terminal write", None, RuntimeError("synthetic store refusal"))
        if refusal_kind == "store"
        else StateCompatibilityRefused("update_status", "synthetic-profile", "synthetic refusal")
        if refusal_kind == "compatibility"
        else None
    )
    original_prepare = state.module._prepare_worker_spawn_continuation

    async def on_setup(ctx):
        event = ctx["worker_spawn_owner"]._drained
        original_wait = event.wait

        async def observed_wait():
            drain_started.set()
            try:
                return await original_wait()
            except asyncio.CancelledError as exc:
                observed_cancel.append(exc)
                cancelled_in_wait.set()
                raise

        monkeypatch.setattr(event, "wait", observed_wait)

    async def operate(owner, turn, ctx):
        admission = owner.admit()

        async def record_later():
            try:
                await release.wait()
                await admission.record(_identity())
            finally:
                admission.finish()

        child = asyncio.create_task(record_later())
        admission.retain(child)
        retained.append(child)
        raise TimeoutError("synthetic timeout")

    async def close(db):
        if state.contexts and db is state.contexts[0]["db"]:
            assert all(task.done() for task in retained), (
                "bound close must follow the admitted work"
            )
            closed.append(db)
        await original_close(db)

    async def update_status(db, *args, **kwargs):
        if (
            refusal is not None
            and state.contexts
            and db is state.contexts[0]["db"]
            and args[:2] == ("session", state.contexts[0]["session_id"])
            and kwargs.get("new_status") == "cancelled"
        ):
            raise refusal
        return await original_status(db, *args, **kwargs)

    def prepare(*args, **kwargs):
        prepared.append(True)
        return original_prepare(*args, **kwargs)

    state.operate, state.on_setup = operate, on_setup
    monkeypatch.setattr(store.StateDB, "close", close)
    monkeypatch.setattr(store.StateDB, "update_status", update_status)
    monkeypatch.setattr(state.module, "_prepare_worker_spawn_continuation", prepare)
    task = asyncio.create_task(
        state.module._run_agent(
            "claude_code/sonnet",
            "hello",
            timeout=30,
            resume_on_timeout=True,
            no_mcp_config=True,
        )
    )
    try:
        await asyncio.wait_for(drain_started.wait(), 5)
        assert len(retained) == 1 and not retained[0].done()
        task.cancel()
        await asyncio.wait_for(cancelled_in_wait.wait(), 5)
        assert not task.done() and closed == [] and prepared == []
        async with store.StateDB() as check:
            assert (await check.get_session(state.contexts[0]["session_id"]))["status"] == "running"
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await _join(task)
        assert task.cancelled()
        assert raised.value is observed_cancel[0] or raised.value.__context__ is observed_cancel[0]
        assert retained[0].done() and closed and prepared == [] and len(state.contexts) == 1
        ctx = state.contexts[0]
        assert ctx["worker_spawn_db_closed"] is ctx["db"]
        async with store.StateDB() as check:
            actual_row = await check.get_session(ctx["session_id"])
        manifests = [json.loads(value) for value in state.manifests]
        final_manifest = manifests[-1]
        assert final_manifest["status"] == ctx["worker_spawn_teardown_status"]
        if refusal_kind == "store":
            assert ctx["teardown_store_error"] is refusal
            assert actual_row["status"] == final_manifest["status"] == "running"
            assert final_manifest["ended_at"] is None
            assert final_manifest["terminal_write_lost"]["status"] == "cancelled"
            assert final_manifest["terminal_write_lost"]["reason_code"]
        elif refusal_kind == "compatibility":
            assert ctx["store_refusal"] is refusal
            assert actual_row["status"] == "running"
            assert final_manifest["status"] == "failed"
            assert "The store refused a write" in final_manifest["error"]
            assert "terminal_write_lost" not in final_manifest
        else:
            assert actual_row["status"] == final_manifest["status"] == "cancelled"
            assert "terminal_write_lost" not in final_manifest
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *retained, return_exceptions=True)
