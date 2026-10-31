# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Every path a refused write can travel, and what each must do with it (ADR-0128 D6).

A refusal is not an ordinary failure: no persistence fallback may swallow it, and a refused write
must never become a run that succeeded without persistence. Each test here drives one of the
fallbacks that can see a ``StateCompatibilityRefused`` and asserts the refusal is raised as itself
rather than folded into a partial or earlier result. The store helpers come from the compatibility
mode suite, so a refusal is the one the real store raises.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.ln import AlcallParams
from lionagi.state.compat import (
    Closure,
    CompatibilityProfile,
    StateCompatibilityRefused,
)
from tests.state.test_compatibility_mode import (
    _open,
    _profile,
    _store,
)

# A minimal, valid profile: what a store stand-in would carry in compatibility mode.
_PROFILE = CompatibilityProfile(
    name="synthetic", version=1, closure=Closure(tables=()), writes={}, operations={"op"}
)


async def _refusing_handle(tmp_path: Path, name: str):
    """An open compatibility-mode handle on a managed store, and the store's own refusal."""
    store = _store(tmp_path / f"{name}-managed.db")
    reference = _store(tmp_path / f"{name}-reference.db")
    return await _open(store, _profile(reference))


async def _refusing_admission_handle(tmp_path: Path, name: str):
    """A refusing handle whose fresh-key read answers absent.

    Admission reads the new session key before it writes. A managed store that holds the
    profile's session table answers that read with no row; the synthetic store holds only its
    own tables, so the read is answered here and the first admission write is what the store
    refuses.
    """
    handle = await _refusing_handle(tmp_path, name)

    async def no_row(session_id):
        return None

    handle.get_session = no_row
    return handle


# 1. ReAct: a refused final turn is raised, not answered with an earlier round's reply


async def test_a_final_operate_the_store_refuses_is_raised(tmp_path: Path):
    """The final ``operate`` writes through the store and is refused, so the run stops instead of
    returning the previous round's reply as this call's answer (ADR-0128 D6)."""
    from unittest.mock import patch

    from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis
    from lionagi.testing import LionAGIMockFactory

    handle = await _refusing_handle(tmp_path, "react")
    branch = LionAGIMockFactory.create_mocked_branch(
        name="refusal", user="tester", response="mocked", model="gpt-4.1-mini"
    )
    await branch.msgs.a_add_message(assistant_response="an earlier round's answer")

    class _Round(ReActAnalysis):
        extension_needed: bool = False

    calls: list[int] = []

    async def operate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _Round(analysis="reasoning about the task")
        # The final turn's own write, refused by the store rather than stubbed.
        await handle.run_sql("DELETE FROM runs")
        return Analysis(answer="must never be returned")

    try:
        with patch("lionagi.operations.operate.operate.operate", new=operate):
            with pytest.raises(StateCompatibilityRefused, match="DELETE"):
                await branch.ReAct(
                    instruct={"instruction": "do the thing"}, extension_allowed=False
                )
    finally:
        await handle.close()
    assert len(calls) == 2, "the refused final turn is the one that had to be reached"


# 2. Orchestration admission: a refused admission write fails the run with its reason


async def test_a_refusal_inside_a_group_fails_the_orchestration_run(tmp_path: Path, monkeypatch):
    """A refusal that arrives inside an exception group fails the run with the refusal as its
    reason, as a bare one does, rather than recording persistence as degraded."""
    from lionagi import Session
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration
    from lionagi.ln.concurrency._compat import BaseExceptionGroup

    refusal = StateCompatibilityRefused("create_progression", "owned@1", "refused")

    async def grouped(**_):
        raise BaseExceptionGroup("setup", [refusal, ValueError("other")])

    monkeypatch.setattr(_orchestration, "_open_shared_db", grouped)
    run = RunDir(
        run_id="grouped-refusal",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    manifest: dict = {"status": "running"}
    run.write_manifest(manifest)

    with pytest.raises(BaseExceptionGroup):
        await _orchestration.setup_orchestration_persist(Session(), run=run, run_manifest=manifest)

    assert manifest["status"] == "failed"
    assert manifest["error"] == f"The store refused a write: {refusal}"
    assert "persistence_degraded_reason" not in manifest


async def test_a_refused_orchestration_admission_fails_the_run(tmp_path: Path, monkeypatch):
    """An admission write the store refuses fails the run with the refusal as its reason, rather
    than recording persistence as degraded and continuing without it (ADR-0128 D6)."""
    from lionagi import Session
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration

    handle = await _refusing_admission_handle(tmp_path, "admission")
    monkeypatch.setattr(_orchestration, "_open_shared_db", lambda **_: _resolved(handle))
    monkeypatch.setattr(_orchestration, "_SQLITE_ADMISSION_RETRY_DELAYS", (0.0, 0.0))
    # The run-identity markers read host process facts a restricted environment can deny; fix
    # them so the admission write, not the marker read, is what the store refuses.
    monkeypatch.setattr(
        "lionagi.ln._proc.current_pid_markers",
        lambda: {
            "pid": 1,
            "pid_create_time": 1.0,
            "pid_host": "here",
            "pid_boot_time": 1.0,
            "process_identity_mode": "local",
        },
    )

    run = RunDir(
        run_id="refused-admission",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    manifest: dict = {"status": "running"}
    run.write_manifest(manifest)

    try:
        with pytest.raises(StateCompatibilityRefused) as caught:
            await _orchestration.setup_orchestration_persist(
                Session(), run=run, run_manifest=manifest
            )
    finally:
        await handle.close()

    # The refusal came from one of the two admission writes, not from a later step.
    assert caught.value.operation == "create_progression"
    assert manifest["status"] == "failed"
    assert manifest["error"] == f"The store refused a write: {caught.value}"
    assert "persistence_degraded_reason" not in manifest
    written = json.loads(run.manifest_path.read_text())
    assert written["status"] == "failed"
    assert written["error"] == manifest["error"]
    assert "persistence_degraded_reason" not in written


async def test_a_refused_admission_without_a_manifest_leaves_run_json_as_it_was(
    tmp_path: Path, monkeypatch
):
    """With no manifest of its own to update, the refusal branch writes nothing over run.json."""
    from lionagi import Session
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration

    handle = await _refusing_admission_handle(tmp_path, "admission-no-manifest")
    monkeypatch.setattr(_orchestration, "_open_shared_db", lambda **_: _resolved(handle))
    monkeypatch.setattr(_orchestration, "_SQLITE_ADMISSION_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(
        "lionagi.ln._proc.current_pid_markers",
        lambda: {
            "pid": 1,
            "pid_create_time": 1.0,
            "pid_host": "here",
            "pid_boot_time": 1.0,
            "process_identity_mode": "local",
        },
    )

    run = RunDir(
        run_id="refused-admission-no-manifest",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    run.write_manifest({"status": "running", "kind": "orchestrate"})
    before = json.loads(run.manifest_path.read_text())

    try:
        with pytest.raises(StateCompatibilityRefused):
            await _orchestration.setup_orchestration_persist(Session(), run=run, run_manifest=None)
    finally:
        await handle.close()

    assert json.loads(run.manifest_path.read_text()) == before


async def _resolved(value):
    return value


async def test_a_non_refusal_from_admission_degrades_the_run(tmp_path: Path, monkeypatch):
    """An ordinary error while opening the store reaches orchestration admission as a setup
    failure: the run records persistence degraded and keeps running. Only a store refusal fails
    the run, so the admission path must not fail the run on an error that is not one."""
    from lionagi import Session
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate import _orchestration

    async def broken_open(**_):
        raise RuntimeError("the store would not open")

    monkeypatch.setattr(_orchestration, "_open_shared_db", broken_open)
    run = RunDir(
        run_id="degraded-admission",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    manifest: dict = {"status": "running"}
    run.write_manifest(manifest)

    ctx = await _orchestration.setup_orchestration_persist(
        Session(), run=run, run_manifest=manifest
    )

    assert ctx is None
    assert manifest["status"] == "running", "an ordinary setup error failed the run"
    assert "the store would not open" in manifest["persistence_degraded_reason"]


async def test_orchestration_admission_leaves_the_registered_shared_handle_in_place(
    tmp_path: Path, monkeypatch
):
    """Orchestration writes through its own handle and never replaces the process's shared one,
    so a handle another owner registered is neither closed nor displaced by this setup."""
    import lionagi.state.db as db_mod
    from lionagi import Session
    from lionagi.cli._runs import RunDir, teardown_persist
    from lionagi.cli.orchestrate import _orchestration
    from lionagi.state.db import StateDB, get_shared_db, register_shared_db

    monkeypatch.setattr(db_mod, "_SHARED", {})
    monkeypatch.setattr(db_mod, "_SHARED_OPEN_LOCK", None)
    store = tmp_path / "orch.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    monkeypatch.setattr(
        "lionagi.ln._proc.current_pid_markers",
        lambda: {
            "pid": 1,
            "pid_create_time": 1.0,
            "pid_host": "here",
            "pid_boot_time": 1.0,
            "process_identity_mode": "local",
        },
    )

    registered = StateDB(store)
    await registered.open()
    await register_shared_db(registered)
    closes: list[int] = []

    async def counted_close():
        closes.append(1)

    run = RunDir(
        run_id="no-displacement",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    manifest: dict = {"status": "running"}
    run.write_manifest(manifest)

    ctx = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(registered, "close", counted_close)
            ctx = await _orchestration.setup_orchestration_persist(
                Session(), run=run, run_manifest=manifest
            )
            assert closes == [], "orchestration setup closed a handle another owner registered"
            assert await get_shared_db(store) is registered
            assert ctx is not None and ctx["db"] is not registered
    finally:
        if ctx is not None:
            await teardown_persist(ctx, status="completed")
        await registered.close()
    assert manifest["status"] == "running"


# 3. Flow: a refused write settles the sibling held behind the fan-out's semaphore


class _Statuses:
    """Records the on_progress status of every operation, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, op_id: str, name: str, status: str, elapsed: float) -> None:
        self.calls.append((str(op_id), status))

    def for_op(self, op_id) -> list[str]:
        seen = str(op_id)
        return [status for op, status in self.calls if op == seen]


def _stranding_flow():
    """A fan whose first operation refuses, whose second occupies the one slot, and a third parked
    behind it: the shape in which a refusal strands an announced sibling.

    ``AlcallParams(max_concurrent=1)`` admits one operation at a time. The refuser takes the slot
    and raises, the operation holding the slot is cancelled, and the third waits for a slot it will
    never get -- parked in the fan-out's own semaphore, so it never reaches the per-operation
    handler that would settle it, and it keeps whatever it last reported unless the refusal path
    sweeps it.
    """
    import anyio

    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    ran: list[str] = []
    held = anyio.Event()

    async def refused(**kw):
        ran.append("refused")
        raise StateCompatibilityRefused("persist_live_message", "messages@1", "refused")

    async def holder(**kw):
        ran.append("holder")
        await held.wait()
        return "holder"

    async def sibling(**kw):
        ran.append("sibling")
        return "sibling"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("refused", refused)
    session.register_operation("holder", holder)
    session.register_operation("sibling", sibling)
    builder = OperationGraphBuilder()
    refused_id = builder.add_operation("refused", depends_on=[], node_id="refuser")
    builder.add_operation("holder", depends_on=[], node_id="holder")
    sibling_id = builder.add_operation("sibling", depends_on=[], node_id="sibling")
    return session, builder.get_graph(), refused_id, sibling_id, ran


def _cancel_path_word(graph) -> str:
    """The last signal the cancellation path records for an announced operation it never reaches.

    Built on its own executor over the same graph, so the word asserted against the refusal path is
    read from the cancellation path's own behavior rather than restated by hand.
    """
    from lionagi.operations.flow import DependencyAwareExecutor
    from lionagi.session.session import Session

    probe = DependencyAwareExecutor(session=Session(), graph=graph)
    log = _Statuses()
    probe.on_progress = log
    node = next(iter(graph.internal_nodes.values()))
    probe._queued_announced[node.id] = node
    probe._cancel_announced_unfinished()
    return log.for_op(node.id)[-1]


async def test_a_refusal_settles_the_sibling_parked_behind_the_fan_out():
    """A refused write stops the flow rather than letting the sibling run on."""
    from lionagi.operations import flow

    session, graph, refused_id, sibling_id, ran = _stranding_flow()
    log = _Statuses()

    with pytest.raises(StateCompatibilityRefused, match="refused"):
        await flow(
            session,
            graph,
            alcall_params=AlcallParams(max_concurrent=1),
            on_progress=log,
        )

    assert ran == ["refused", "holder"], "the parked sibling must never have run"
    assert log.for_op(refused_id)[-1] == "failed"
    assert log.for_op(sibling_id)[-1] != "queued"
    assert log.for_op(sibling_id)[-1] == _cancel_path_word(graph)


# 4. Flow: every failure beside the one that is raised is logged


def test_every_failure_beside_the_refusal_is_logged(caplog):
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.operations.flow import _refusal

    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "first refused")
    other = ValueError("an ordinary failure")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [refusal, other])

    with caplog.at_level(logging.WARNING, logger="lionagi.operations.flow"):
        assert _refusal(group) is refusal

    (record,) = [r for r in caplog.records if "an ordinary failure" in r.getMessage()]
    assert record.levelname == "WARNING"


def test_a_second_refusal_is_logged_beside_the_one_that_is_raised(caplog):
    """One refusal stops the flow; a second one in the same group is reported rather than dropped
    without a line, the way the teardown reports refusals after the first (ADR-0128 D6)."""
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.operations.flow import _refusal

    first = StateCompatibilityRefused("persist_live_message", "messages@1", "first refused")
    second = StateCompatibilityRefused("persist_live_message", "messages@2", "second refused")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [first, second])

    with caplog.at_level(logging.WARNING, logger="lionagi.operations.flow"):
        assert _refusal(group) is first

    (record,) = [r for r in caplog.records if "second refused" in r.getMessage()]
    assert record.levelname == "WARNING"


# 5. act: a refusal raised by the tool-post hook is raised under suppression


async def test_a_refused_tool_post_write_is_raised_under_suppression():
    """``suppress_errors=True`` swallows ordinary tool failures; a refused write by the post-hook
    path is not one of them (ADR-0128 D6)."""
    from lionagi.hooks.bus import HookBus, HookPoint
    from lionagi.operations.act.act import _act
    from lionagi.protocols.generic.event import EventStatus

    bus = HookBus()

    async def refused(**kw):
        raise StateCompatibilityRefused("persist_live_message", "messages@1", "refused")

    bus.on(HookPoint.TOOL_POST, refused)
    branch = MagicMock()
    branch._hooks = bus
    branch.id = "branch-test"
    branch.authorize = AsyncMock(return_value=True)
    branch._log_manager.log = MagicMock()
    branch.msgs.a_add_message = AsyncMock()
    branch.emit_and_log = AsyncMock()
    func_call = MagicMock()
    func_call.status = EventStatus.COMPLETED
    func_call.execution.duration = 0.01
    branch._action_manager.invoke = AsyncMock(return_value=func_call)

    with pytest.raises(StateCompatibilityRefused, match="refused"):
        await _act(branch, {"function": "my_tool", "arguments": {}}, suppress_errors=True)


# 6. message callbacks: a refusal beside another error is raised as itself


def _manager_with_callbacks(refusal: StateCompatibilityRefused, async_callbacks: bool):
    from lionagi.protocols.types import MessageManager

    manager = MessageManager()

    if async_callbacks:

        async def refuse(msg):
            raise refusal

        async def boom(msg):
            raise ValueError("an ordinary callback failure")

    else:

        def refuse(msg):
            raise refusal

        def boom(msg):
            raise ValueError("an ordinary callback failure")

    manager._on_message_added.extend([boom, refuse])
    return manager


async def test_a_refusal_beside_another_callback_error_is_raised():
    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    manager = _manager_with_callbacks(refusal, async_callbacks=True)

    with pytest.raises(StateCompatibilityRefused) as caught:
        await manager.a_add_message(instruction="hi", sender="u", recipient="r")
    assert caught.value is refusal


def test_a_refusal_beside_another_callback_error_is_raised_on_the_sync_path():
    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    manager = _manager_with_callbacks(refusal, async_callbacks=False)

    with pytest.raises(StateCompatibilityRefused) as caught:
        manager.add_message(instruction="hi", sender="u", recipient="r")
    assert caught.value is refusal


def test_a_refusal_keeps_its_own_cause_beside_another_callback_error(caplog):
    """The refusal is raised with the cause it was raised from, and the other callback's error
    is logged rather than dropped."""
    refusal = StateCompatibilityRefused("open", "messages@1", "the store could not be read")
    original = OSError("disk I/O error")
    refusal.__cause__ = original
    manager = _manager_with_callbacks(refusal, async_callbacks=False)

    with caplog.at_level(logging.WARNING, logger="lionagi.protocols.messages.manager"):
        with pytest.raises(StateCompatibilityRefused) as caught:
            manager.add_message(instruction="hi", sender="u", recipient="r")
    assert caught.value is refusal
    assert caught.value.__cause__ is original
    assert "an ordinary callback failure" in caplog.text


def test_a_refusal_is_found_inside_nested_groups():
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.state.compat import refusal_in

    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    group = BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ValueError("an ordinary failure"), BaseExceptionGroup("nested", [refusal])],
    )

    assert refusal_in(group) is refusal
    assert refusal_in(refusal) is refusal
    assert refusal_in(ValueError("nothing here")) is None
    assert refusal_in(None) is None


# 7. Compatibility mode is read from the profile the store sets, not any non-None value


def test_only_a_profile_marks_a_store_as_compatibility_mode():
    """A store exposes the profile it was opened with; anything else in that attribute -- a bare
    string, a double -- is not evidence of compatibility mode, so the native path is taken."""
    from lionagi.state.compat import is_compatibility_store

    assert is_compatibility_store(MagicMock(compatibility=_PROFILE)) is True
    assert is_compatibility_store(MagicMock(compatibility=None)) is False
    assert is_compatibility_store(MagicMock(compatibility=MagicMock())) is False
    assert is_compatibility_store(MagicMock(compatibility="profile")) is False


async def test_a_stand_in_whose_compatibility_is_a_mock_takes_the_native_path():
    from lionagi.hooks._message_retry import MessagePersistRetryQueue, PendingMessageEvent

    class _Locked:
        def __init__(self, compatibility) -> None:
            self.compatibility = compatibility
            self.calls = 0

        async def _persist_live_message(self, message, **kwargs) -> None:
            self.calls += 1
            raise RuntimeError("database is locked")

    event = PendingMessageEvent(
        message={"id": "m1", "content": "x", "role": "user"}, session_id="s"
    )

    # A double for the profile is not one, so the queue defers after three failures and stops
    # attempting its head on later submits: the native behaviour.
    native = _Locked(MagicMock())
    queue = MessagePersistRetryQueue(native, logger=logging.getLogger("test"), owner="b1")
    for _ in range(4):
        assert await queue.submit(event) is False
    assert native.calls == 3

    # A real profile keeps the compatibility behaviour: each submit still attempts the head.
    compat = _Locked(_PROFILE)
    compat_queue = MessagePersistRetryQueue(compat, logger=logging.getLogger("test"), owner="b1")
    for _ in range(4):
        assert await compat_queue.submit(event) is False
    assert compat.calls == 4


# 8. act: a refusal a call or hook wrapped in a group is still raised as itself


async def test_a_refusal_inside_a_group_from_the_call_leaves_as_itself():
    """A task group wrapping the refusal must not hide it from ``_act()``: the refusal is
    raised as itself, not recorded as a tool error (ADR-0128 D6)."""
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.operations.act.act import _act

    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    group = BaseExceptionGroup("the tool call failed", [ValueError("ordinary"), refusal])
    branch = MagicMock()
    branch._hooks = None
    branch.id = "branch-test"
    branch.authorize = AsyncMock(return_value=True)
    branch._log_manager.log = MagicMock()
    branch.msgs.a_add_message = AsyncMock()
    branch.emit_and_log = AsyncMock()
    branch._action_manager.invoke = AsyncMock(side_effect=group)

    with pytest.raises(StateCompatibilityRefused) as caught:
        await _act(branch, {"function": "my_tool", "arguments": {}}, suppress_errors=True)
    assert caught.value is refusal


async def test_a_group_without_a_refusal_still_degrades_to_a_tool_error():
    """A group of ordinary tool failures is suppressed exactly as a single one was."""
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.operations.act.act import _act

    group = BaseExceptionGroup("the tool call failed", [ValueError("one"), OSError("two")])
    branch = MagicMock()
    branch._hooks = None
    branch.id = "branch-test"
    branch.authorize = AsyncMock(return_value=True)
    branch._log_manager.log = MagicMock()
    branch.msgs.a_add_message = AsyncMock()
    branch.emit_and_log = AsyncMock()
    branch._action_manager.invoke = AsyncMock(side_effect=group)

    response = await _act(branch, {"function": "my_tool", "arguments": {}}, suppress_errors=True)

    assert response.output is not None
    assert "error" in response.output


# 9. setup: a refusal inside a group is raised as itself, and an ordinary group is unchanged


async def test_a_setup_refusal_inside_a_group_leaves_as_itself(monkeypatch):
    from lionagi.cli import _runs as runs_mod
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.session.branch import Branch

    refusal = StateCompatibilityRefused("create_session", "synthetic", "refused")
    group = BaseExceptionGroup("setup writes", [ValueError("ordinary"), refusal])

    async def refuse(*args, **kwargs):
        raise group

    monkeypatch.setattr(runs_mod, "_open_shared_db", refuse)

    with pytest.raises(StateCompatibilityRefused) as caught:
        await runs_mod.setup_agent_persist(Branch(), agent_name="refusal-group")
    assert caught.value is refusal


async def test_a_setup_group_without_a_refusal_still_degrades_persistence(monkeypatch):
    from lionagi.cli import _runs as runs_mod
    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.session.branch import Branch

    group = BaseExceptionGroup("setup writes", [ValueError("one"), OSError("two")])

    async def fail(*args, **kwargs):
        raise group

    monkeypatch.setattr(runs_mod, "_open_shared_db", fail)

    assert await runs_mod.setup_agent_persist(Branch(), agent_name="plain-group") is None


# 10. ReAct: a refusal inside a group from the final call is raised as itself


async def test_a_final_operate_refusal_inside_a_group_leaves_as_itself(tmp_path: Path):
    """The caller's ``isinstance`` check must still find the refusal when a task group
    wrapped it beside another failure."""
    from unittest.mock import patch

    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.operations.ReAct.utils import ReActAnalysis
    from lionagi.testing import LionAGIMockFactory

    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    group = BaseExceptionGroup("the final call failed", [ValueError("ordinary"), refusal])
    branch = LionAGIMockFactory.create_mocked_branch(
        name="refusal-group", user="tester", response="mocked", model="gpt-4.1-mini"
    )
    await branch.msgs.a_add_message(assistant_response="an earlier round's answer")

    class _Round(ReActAnalysis):
        extension_needed: bool = False

    calls: list[int] = []

    async def operate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _Round(analysis="reasoning about the task")
        raise group

    with patch("lionagi.operations.operate.operate.operate", new=operate):
        with pytest.raises(StateCompatibilityRefused) as caught:
            await branch.ReAct(instruct={"instruction": "do the thing"}, extension_allowed=False)
    assert caught.value is refusal


async def test_a_final_operate_group_without_a_refusal_still_falls_back(tmp_path: Path):
    from unittest.mock import patch

    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.operations.ReAct.utils import ReActAnalysis
    from lionagi.testing import LionAGIMockFactory

    group = BaseExceptionGroup("the final call failed", [ValueError("one"), OSError("two")])
    branch = LionAGIMockFactory.create_mocked_branch(
        name="plain-group", user="tester", response="mocked", model="gpt-4.1-mini"
    )
    await branch.msgs.a_add_message(assistant_response="an earlier round's answer")
    expected = branch.msgs.last_response.response

    class _Round(ReActAnalysis):
        extension_needed: bool = False

    calls: list[int] = []

    async def operate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _Round(analysis="reasoning about the task")
        raise group

    with patch("lionagi.operations.operate.operate.operate", new=operate):
        result = await branch.ReAct(
            instruct={"instruction": "do the thing"}, extension_allowed=False
        )
    assert result == expected


# 11. message callbacks: a cancellation beside the refusal is not reported as an error


def test_a_cancellation_beside_a_refusal_is_not_logged(caplog):
    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    manager = _manager_with_cancellation(refusal)

    with caplog.at_level(logging.WARNING, logger="lionagi.protocols.messages.manager"):
        with pytest.raises(StateCompatibilityRefused) as caught:
            manager.add_message(instruction="hi", sender="u", recipient="r")

    assert caught.value is refusal
    assert "beside a store refusal" not in caplog.text


def _manager_with_cancellation(refusal: StateCompatibilityRefused):
    import asyncio

    from lionagi.protocols.types import MessageManager

    manager = MessageManager()

    def refuse(msg):
        raise refusal

    def cancelled(msg):
        raise asyncio.CancelledError

    manager._on_message_added.extend([cancelled, refuse])
    return manager


# Parse: a refused write in the repair turn stops the run


@pytest.mark.parametrize("handle_validation", ["raise", "return_value", "return_none"])
async def test_a_refused_write_in_the_parse_repair_turn_is_raised(handle_validation, monkeypatch):
    """parse retries an unparseable reply through ``Branch.chat``; a refused write there stops
    the run instead of becoming the raw text, None, or a parse error a caller may retry."""
    from pydantic import BaseModel

    from lionagi.operations.parse.parse import parse
    from lionagi.operations.types import ParseParam
    from lionagi.session.branch import Branch

    class Answer(BaseModel):
        n: int

    refusal = StateCompatibilityRefused("persist_live_message", "synthetic@1", "refused")

    async def refused_chat(self, **kwargs):
        raise refusal

    monkeypatch.setattr(Branch, "chat", refused_chat)
    with pytest.raises(StateCompatibilityRefused) as raised:
        await parse(
            Branch(name="repair"),
            "not json",
            ParseParam(response_format=Answer, handle_validation=handle_validation),
        )
    assert raised.value is refusal


# Flow: a refusal inside an operation's error group stops the flow


@pytest.mark.parametrize("entry", ["dependency-aware", "reactive", "stream"])
async def test_a_refusal_inside_an_operation_error_group_stops_the_flow(entry):
    """An operation whose call raised a group holding the refusal stops the flow with that
    refusal, rather than failing the one node while its sibling runs on."""
    import anyio

    from lionagi.ln.concurrency import BaseExceptionGroup
    from lionagi.operations import flow
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    refusal = StateCompatibilityRefused("persist_live_message", "synthetic@1", "refused")

    async def grouped(**kwargs):
        raise BaseExceptionGroup("the operation's call failed", [ValueError("ordinary"), refusal])

    async def slow(**kwargs):
        await anyio.sleep(0.5)
        return "slow"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("grouped", grouped)
    session.register_operation("slow", slow)
    builder = OperationGraphBuilder()
    builder.add_operation("grouped", depends_on=[])
    builder.add_operation("slow", depends_on=[])
    graph = builder.get_graph()

    with pytest.raises(StateCompatibilityRefused) as raised:
        if entry == "stream":
            async for _ in session.flow_stream(graph):
                pass
        else:
            await flow(session, graph, reactive=entry == "reactive")
    assert raised.value is refusal
