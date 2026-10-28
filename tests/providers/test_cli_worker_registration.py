# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import socket
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio

_GENERATION = "902646b9-53e2-4863-952f-da579eb0dabc"
_OTHER_GENERATION = "c06b1529-665f-4ee4-9ff1-c85d5fb2d9da"


@pytest_asyncio.fixture(loop_scope="function")
async def scripted(monkeypatch):
    import psutil

    from lionagi.providers import _cli_subprocess as cs
    from lionagi.state.cli_workers import _WorkerSpawnOwner

    state = SimpleNamespace(
        cs=cs,
        events=[],
        tasks=[],
        owners=[],
        admissions=[],
        observations=[],
        observation_times=[1.0],
        spawn_entered=asyncio.Event(),
        spawn_allowed=asyncio.Event(),
        record_entered=asyncio.Event(),
        record_allowed=asyncio.Event(),
        cleanup_entered=asyncio.Event(),
        cleanup_allowed=asyncio.Event(),
        first_io=asyncio.Event(),
        group_present=False,
        store_error=None,
        spawn_error=None,
        records=[],
        released=0,
        kept=0,
        program=sys.executable,
        spawn_hook=None,
        cleanup_error=None,
    )
    for gate in (state.spawn_allowed, state.record_allowed, state.cleanup_allowed):
        gate.set()

    class Pipe:
        def __init__(self, name, chunks):
            self.name, self.chunks = name, list(chunks)

        async def read(self, _size):
            state.events.append(self.name)
            state.first_io.set()
            return self.chunks.pop(0) if self.chunks else b""

    class Input:
        def write(self, data):
            assert data == b"input"
            state.events.append("stdin")
            state.first_io.set()

        async def drain(self):
            return None

        def close(self):
            state.events.append("stdin-close")

    class Child:
        pid = 12345
        returncode = None
        stdout = Pipe("stdout", [b'{"ok": true}\n', b""])
        stderr = Pipe("stderr", [b""])
        stdin = Input()

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            state.events.append("kill")

    state.child = Child()

    class Lease:
        slots = []

        def record_child(self, pid, created):
            assert pid == state.child.pid
            state.events.append("lease-identity")

        def release(self):
            state.released += 1

        def keep_for_child(self):
            state.kept += 1
            raise AssertionError("unconfirmed groups must retain their holder locks")

    state.lease = Lease()

    class Store:
        async def register_cli_worker_spawn(self, record):
            state.events.append("record-enter")
            state.record_entered.set()
            await state.record_allowed.wait()
            if state.store_error is not None:
                raise state.store_error
            state.records.append(dict(record))
            state.events.append("record-commit")

    state.store = Store()

    def owner(db=None, *, generation=_GENERATION):
        result = _WorkerSpawnOwner(
            state.store if db is None else db,
            session_id="persisted-owner",
            generation_id=generation,
            run_id="diagnostic-run",
        )
        real_admit = result.admit

        def admitted():
            token = real_admit()
            state.admissions.append(token)
            return token

        monkeypatch.setattr(result, "admit", admitted)
        state.owners.append(result)
        return result

    state.owner = owner
    real_ensure_future = asyncio.ensure_future

    def ensure_future(coro, *args, **kwargs):
        created = real_ensure_future(coro, *args, **kwargs)
        code = getattr(coro, "cr_code", None)
        if code is not None and code.co_name in {
            "_spawn_and_observe",
            "_spawn_and_register",
            "_cleanup_abandoned_spawn",
        }:
            state.tasks.append(created)
        return created

    def task(coro):
        created = asyncio.create_task(coro)
        state.tasks.append(created)
        return created

    state.task = task

    async def admit(**_kwargs):
        return state.lease

    async def fill(env):
        return env, frozenset()

    async def spawn(*_args, **kwargs):
        assert kwargs["start_new_session"] is True
        state.events.append("spawn")
        state.spawn_entered.set()
        await state.spawn_allowed.wait()
        if state.spawn_hook is not None:
            state.spawn_hook()
        if state.spawn_error is not None:
            raise state.spawn_error
        return state.child

    def observe(pid, *, expected_marker):
        assert pid == state.child.pid and expected_marker == "synthetic-marker"
        index = min(len(state.observations), len(state.observation_times) - 1)
        created = state.observation_times[index]
        state.observations.append(created)
        return cs.SpawnedProcess(pid, pid, created)

    async def end(child, *, spawned):
        assert child is state.child
        state.events.append("cleanup")
        state.cleanup_entered.set()
        await state.cleanup_allowed.wait()
        child.returncode = 0
        if state.cleanup_error is not None:
            raise state.cleanup_error
        outcome = getattr(cs, "_group_cleanup_outcome", None)
        return outcome(child, spawned) if outcome is not None else None

    async def poll(child):
        assert child is state.child
        state.cleanup_entered.set()
        await state.cleanup_allowed.wait()
        child.returncode = 0

    monkeypatch.setattr(cs, "admit_subagent", admit)
    monkeypatch.setattr(cs, "fill_declared_secrets_and_names", fill)
    monkeypatch.setattr(cs, "refuse_nested_agent", lambda _engine: None)
    monkeypatch.setattr(cs, "child_agent_depth", lambda: 0)
    monkeypatch.setattr(
        cs, "prepare_process_group_env", lambda env: (dict(env or {}), "synthetic-marker")
    )
    monkeypatch.setattr(cs, "observe_spawned", observe)
    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cs.asyncio, "ensure_future", ensure_future)
    monkeypatch.setattr(cs, "end_child_group", end)
    monkeypatch.setattr(cs, "_wait_owned_child_exit", poll, raising=False)
    monkeypatch.setattr(cs, "_group_has_members", lambda _pgid: state.group_present, raising=False)
    monkeypatch.setattr(cs, "_witnesses_for_spawn", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(cs, "signal_group_if_witnessed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(socket, "gethostname", lambda: "synthetic-host")
    monkeypatch.setattr(psutil, "boot_time", lambda: 0.5)
    previous_tasks = set(getattr(cs, "_RETAINED_CLI_TASKS", ()))
    try:
        yield state
    finally:
        for gate in (state.spawn_allowed, state.record_allowed, state.cleanup_allowed):
            gate.set()
        joined = set()
        while True:
            retained = set(getattr(cs, "_RETAINED_CLI_TASKS", ())) - previous_tasks
            tasks = (set(state.tasks) | retained) - joined
            if not tasks:
                break
            joined.update(tasks)
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Only fixture-owned admissions are finalized after all their tasks
        # have joined, so a deliberately broken retention rule cannot hang
        # teardown after its drain assertion has already failed.
        for token in state.admissions:
            token.finish()
        for owned in state.owners:
            await owned.aclose()


async def collect(state):
    def callback(spawned):
        assert spawned.pid == state.child.pid
        state.events.append("callback")

    stream = state.cs.ndjson_from_cli(
        [state.program], env={}, stdin_data=b"input", on_spawn=callback, engine="codex"
    )
    async with contextlib.aclosing(stream):
        return [item async for item in stream]


@pytest.mark.asyncio(loop_scope="function")
async def test_durable_registration_commits_before_callbacks_or_any_transport_read(scripted):
    state = scripted
    owned = state.owner()
    state.record_allowed.clear()

    def observer(child, spawned):
        assert child is state.child and spawned.pid == child.pid
        state.events.append("observer")

    with owned.bind(), state.cs._observe_run_cli_spawns(observer):
        consumer = state.task(collect(state))
    record = state.task(state.record_entered.wait())
    io = state.task(state.first_io.wait())
    completed, _pending = await asyncio.wait(
        {record, io}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
    )
    assert completed, "the scripted spawn must reach registration or transport"
    assert state.record_entered.is_set(), "durable registration must precede first transport read"
    assert (
        not state.first_io.is_set()
        and "callback" not in state.events
        and "observer" not in state.events
    )
    assert not consumer.done()
    state.record_allowed.set()
    assert await asyncio.wait_for(consumer, 1.0) == [{"ok": True}]
    assert (
        state.events.index("record-commit")
        < state.events.index("observer")
        < state.events.index("callback")
    )
    assert all(
        state.events.index("callback") < state.events.index(name)
        for name in ("stdout", "stderr", "stdin")
    )
    assert len(state.records) == 1
    assert (
        state.records[0]["pid_host"],
        state.records[0]["pid_boot_time"],
        state.records[0]["process_identity_mode"],
    ) == ("synthetic-host", 0.5, "local")


@pytest.mark.asyncio(loop_scope="function")
async def test_an_uncapped_persisted_spawn_retries_missing_identity(scripted):
    state = scripted
    state.observation_times = [None, 1.0]
    with state.owner().bind():
        result = await collect(state)
    assert result == [{"ok": True}]
    assert state.observations == [None, 1.0], "a recorder needs identity even without capped slots"
    assert len(state.records) == 1 and state.records[0]["pid_create_time"] == 1.0


@pytest.mark.asyncio(loop_scope="function")
async def test_explicit_none_keeps_uncapped_optional_identity_and_records_nothing(scripted):
    from lionagi._cli_worker_context import _bind_cli_worker_recorder

    state = scripted
    state.observation_times = [None]
    with state.owner().bind(), _bind_cli_worker_recorder(None):
        result = await collect(state)
    assert result == [{"ok": True}] and state.observations == [None]
    assert state.records == []


@pytest.mark.parametrize("group_present", [False, True], ids=["confirmed-empty", "unconfirmed"])
@pytest.mark.asyncio(loop_scope="function")
async def test_registration_refusal_exposes_no_output_and_preserves_lease_ownership(
    scripted, group_present
):
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    state = scripted
    state.lease.slots = [object()]
    state.group_present = group_present
    primary = ValueError("synthetic current-generation refusal")
    state.store_error = primary
    error = None
    result = None
    with state.owner().bind():
        try:
            result = await collect(state)
        except _WorkerSpawnAdmissionError as exc:
            error = exc
    assert result is None and error is not None, "a refused registration must not expose output"
    assert error.__cause__ is primary
    assert state.cleanup_entered.is_set() and not state.first_io.is_set()
    assert "callback" not in state.events and state.kept == 0
    assert state.released == (0 if group_present else 1)


@pytest.mark.parametrize("ending", ["eof", "explicit-close"])
@pytest.mark.asyncio(loop_scope="function")
async def test_unconfirmed_eof_or_explicit_close_surfaces_teardown_failure(scripted, ending):
    from lionagi.providers._provider_errors import ProviderTeardownError

    state = scripted
    state.group_present = True
    state.lease.slots = [object()]
    with state.owner().bind(), pytest.raises(ProviderTeardownError):
        if ending == "eof":
            await collect(state)
        else:
            stream = state.cs.ndjson_from_cli([state.program], env={})
            try:
                assert await stream.__anext__() == {"ok": True}
            finally:
                await stream.aclose()
    assert state.released == 0 and state.kept == 0 and state.lease.slots


@pytest.mark.parametrize("cleanup", ["unconfirmed", "secondary-error"])
@pytest.mark.asyncio(loop_scope="function")
async def test_callback_primary_failure_survives_failed_cleanup(scripted, cleanup):
    state = scripted
    state.group_present = True
    state.lease.slots = [object()]
    primary = ValueError("synthetic callback refusal")
    if cleanup == "secondary-error":
        state.cleanup_error = RuntimeError("synthetic cleanup failure")

    def callback(_spawned):
        raise primary

    with state.owner().bind(), pytest.raises(ValueError) as raised:
        stream = state.cs.ndjson_from_cli([state.program], env={}, on_spawn=callback)
        async with contextlib.aclosing(stream):
            await stream.__anext__()
    assert raised.value is primary
    assert len(state.records) == 1 and not state.first_io.is_set()
    assert state.released == 0 and state.kept == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_a_cancelled_caller_still_registers_the_late_child_and_drains_cleanup(scripted):
    state = scripted
    state.spawn_allowed.clear()
    state.cleanup_allowed.clear()
    owned = state.owner()
    with owned.bind():
        consumer = state.task(collect(state))
    await asyncio.wait_for(state.spawn_entered.wait(), 1.0)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, 1.0)
    closing = asyncio.Event()

    async def close():
        closing.set()
        await owned.aclose()

    closer = state.task(close())
    await asyncio.wait_for(closing.wait(), 1.0)
    assert not closer.done(), "a cancelled caller must not abandon its admitted spawn"
    state.spawn_allowed.set()
    await asyncio.wait_for(state.cleanup_entered.wait(), 1.0)
    assert (
        len(state.records) == 1 and not state.first_io.is_set() and "callback" not in state.events
    )
    assert not closer.done(), "terminal drain must include retained late-child cleanup"
    state.cleanup_allowed.set()
    await asyncio.wait_for(closer, 1.0)
    assert state.released == 1


async def _assert_fast_late_cleanup_is_drained(state):
    state.spawn_allowed.clear()
    owned = state.owner()
    with owned.bind():
        consumer = state.task(collect(state))
    await asyncio.wait_for(state.spawn_entered.wait(), 1.0)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, 1.0)
    # No suspension is needed by the existing witnessed backstop for this
    # already terminal owned child and absent group.
    state.child.returncode = 0
    state.spawn_allowed.set()
    closer = state.task(owned.aclose())
    try:
        done, _pending = await asyncio.wait({closer}, timeout=1.0)
        assert done, "late cleanup must finish its admission before terminal drain returns"
        closer.result()
        assert len(state.records) == 1 and state.released == 1
        assert not state.first_io.is_set() and "callback" not in state.events
    finally:
        if not closer.done():
            # Restore only this fixture's tokens after their tasks finish;
            # the failed assertion above remains the regression result.
            others = [task for task in state.tasks if task is not closer]
            await asyncio.gather(*others, return_exceptions=True)
            for token in state.admissions:
                token.finish()
            await asyncio.wait_for(closer, 1.0)


@pytest.mark.parametrize(
    "failure", ["before-first-step", "after-first-suspension", "cancel-before-first-step"]
)
@pytest.mark.asyncio(loop_scope="function")
async def test_late_cleanup_ownership_survives_task_construction_or_prestart_cancellation(
    scripted, monkeypatch, failure
):
    state = scripted
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    real_ensure_future = asyncio.ensure_future
    reached_closed_gate = []

    def ensure_future(coro, *args, **kwargs):
        if getattr(getattr(coro, "cr_code", None), "co_name", None) != "_cleanup_abandoned_spawn":
            return real_ensure_future(coro, *args, **kwargs)
        if failure == "before-first-step":
            raise RuntimeError("synthetic cleanup task construction failure")
        if failure == "after-first-suspension":
            try:
                yielded = coro.send(None)
            except StopIteration:
                reached_closed_gate.append(False)
            else:
                reached_closed_gate.append(
                    isinstance(yielded, asyncio.Future) and not yielded.done()
                )
            raise RuntimeError("synthetic task construction failure after eager first step")
        task = real_ensure_future(coro, *args, **kwargs)
        task.cancel()
        return task

    monkeypatch.setattr(state.cs.asyncio, "ensure_future", ensure_future)
    try:
        await _assert_fast_late_cleanup_is_drained(state)
        if failure == "after-first-suspension":
            assert reached_closed_gate == [True], (
                "cleanup must first suspend at its closed retention gate"
            )
        assert errors == [], "late cleanup construction/cancellation must not leak callback errors"
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="asyncio.eager_task_factory is 3.12+")
@pytest.mark.asyncio(loop_scope="function")
async def test_late_cleanup_is_retained_before_python312_eager_execution(scripted):
    # Runs only on Python 3.12+. The portable cases above run on every
    # interpreter; a missing eager API is skipped, never counted as PASS.
    eager_factory = asyncio.eager_task_factory
    state = scripted
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    loop.set_task_factory(eager_factory)
    try:
        await _assert_fast_late_cleanup_is_drained(state)
        assert errors == [], (
            "actual eager cleanup must be strongly retained before its body can finish"
        )
    finally:
        loop.set_task_factory(previous_factory)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio(loop_scope="function")
async def test_a_closed_owner_refuses_before_creating_an_os_child(scripted):
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    state = scripted
    owned = state.owner()
    await owned.aclose()
    with owned.bind(), pytest.raises(_WorkerSpawnAdmissionError):
        await collect(state)
    assert not state.spawn_entered.is_set() and state.released == 1


@pytest.mark.parametrize("failure_kind", ["transport", "removed-after-check"])
@pytest.mark.skipif(sys.platform == "win32", reason="configured subagent caps require POSIX flock")
@pytest.mark.asyncio(loop_scope="function")
async def test_an_unknown_no_handle_spawn_failure_keeps_holder_ownership(
    scripted, tmp_path, monkeypatch, caplog, failure_kind
):
    from lionagi import _paths
    from lionagi.providers import _subagent_admission as admission

    state = scripted
    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "isolated-home")
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    leases = []
    replacement = None

    async def admit(**kwargs):
        lease = await admission.admit_subagent(**kwargs, environ=env)
        leases.append(lease)
        return lease

    monkeypatch.setattr(state.cs, "admit_subagent", admit)
    primary = RuntimeError("synthetic failure after attempting OS launch")
    if failure_kind == "removed-after-check":
        executable = tmp_path / "removed-cli"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        state.program = str(executable)
        state.spawn_hook = executable.unlink
        primary = FileNotFoundError(errno.ENOENT, "synthetic failed exec", str(executable))
    state.spawn_error = primary
    owned = state.owner()
    try:
        with owned.bind(), pytest.raises(type(primary)) as raised:
            await collect(state)
        assert raised.value is primary
        await owned.aclose()
        refused = None
        try:
            replacement = await admission.admit_subagent(engine=None, environ=env)
        except admission.SubagentAdmissionRefusedError as exc:
            refused = exc
        assert refused is not None, "an unknown launch outcome must keep actual capacity held"
        assert "launch outcome unconfirmed" in caplog.text and "pools: all" in caplog.text
        assert state.records == [] and not state.first_io.is_set()
    finally:
        if replacement is not None:
            replacement.release()
        for lease in leases:
            lease.release()


@pytest.mark.skipif(sys.platform == "win32", reason="own-session executable resolution is POSIX")
@pytest.mark.parametrize(
    "kind",
    [
        "absolute",
        "relative-argv",
        "relative-path",
        "empty-path",
        "empty-component",
        "absent-path",
        "relative-child-cwd",
        "symlink-child-cwd",
    ],
)
def test_executable_gate_uses_the_child_cwd_and_actual_environment_path(
    tmp_path, monkeypatch, kind
):
    from lionagi.providers import _cli_subprocess as cs

    child = tmp_path / "child"
    child.mkdir()
    directory = child / "bin" if kind in {"relative-argv", "relative-path"} else child
    directory.mkdir(exist_ok=True)
    executable = directory / "gate-cli"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    program, cwd = "gate-cli", child
    env = {"PATH": ""}
    if kind == "absolute":
        program = str(executable)
    elif kind == "relative-argv":
        program, env = "bin/gate-cli", {"PATH": str(tmp_path / "unrelated")}
    elif kind == "relative-path":
        env = {"PATH": "bin"}
    elif kind == "empty-component":
        env = {"PATH": os.pathsep + str(tmp_path / "unrelated")}
    elif kind == "absent-path":
        env = {}
        monkeypatch.setattr(os, "defpath", str(directory))
    elif kind == "relative-child-cwd":
        cwd = os.path.relpath(child)
    elif kind == "symlink-child-cwd":
        nested = child / "nested"
        nested.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        cwd = alias / ".."
    original = dict(env)
    cs._require_cli_executable([program], cwd=cwd, env=env)
    assert env == original, "the executable gate must not rewrite the child's environment"


@pytest.mark.skipif(sys.platform == "win32", reason="own-session executable resolution is POSIX")
@pytest.mark.parametrize("kind", ["parent-path-only", "nonexecutable", "directory"])
def test_executable_gate_refuses_parent_path_fallback_and_nonexecutables(
    tmp_path, monkeypatch, kind
):
    from lionagi.providers import _cli_subprocess as cs
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    child, parent = tmp_path / "child", tmp_path / "parent-bin"
    child.mkdir()
    parent.mkdir()
    executable = (parent if kind == "parent-path-only" else child) / "gate-cli"
    if kind == "directory":
        executable.mkdir()
    else:
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o600 if kind == "nonexecutable" else 0o700)
    monkeypatch.setenv("PATH", str(parent))
    with pytest.raises(_WorkerSpawnAdmissionError, match="unavailable before launch"):
        cs._require_cli_executable(["gate-cli"], cwd=child, env={"PATH": ""})


@pytest.mark.skipif(sys.platform == "win32", reason="configured subagent caps require POSIX flock")
@pytest.mark.parametrize("count", [1, 3], ids=["one", "three"])
@pytest.mark.asyncio(loop_scope="function")
async def test_missing_executables_never_attempt_spawn_or_accumulate_capped_slots(
    tmp_path, monkeypatch, count
):
    from lionagi import _paths
    from lionagi.providers import _cli_subprocess as cs
    from lionagi.providers import _subagent_admission as admission
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "isolated-home")
    env = {"LIONAGI_MAX_SUBAGENTS": str(count), "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    leases, spawns = [], []

    async def admit(**kwargs):
        lease = await admission.admit_subagent(**kwargs, environ=env)
        leases.append(lease)
        return lease

    async def unexpected_spawn(*args, **kwargs):
        spawns.append((args, kwargs))
        raise AssertionError("an unresolved executable must be refused before OS launch")

    async def fill(value):
        return value, frozenset()

    monkeypatch.setattr(cs, "admit_subagent", admit)
    monkeypatch.setattr(cs, "refuse_nested_agent", lambda _engine: None)
    monkeypatch.setattr(cs, "child_agent_depth", lambda: 0)
    monkeypatch.setattr(cs, "fill_declared_secrets_and_names", fill)
    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", unexpected_spawn)
    try:
        for index in range(count):
            stream = cs.ndjson_from_cli([str(tmp_path / f"missing-{index}")], env={})
            with pytest.raises(_WorkerSpawnAdmissionError):
                async with contextlib.aclosing(stream):
                    await stream.__anext__()
        assert spawns == []
        status = admission.subagent_admission_status(environ=env)
        assert status["pools"]["all"]["held"] == 0, (
            "missing executables must leave capped pools empty"
        )
    finally:
        for lease in leases:
            lease.release()


@pytest.mark.parametrize("boot", [None, float("nan")], ids=["unreadable", "nonfinite"])
@pytest.mark.asyncio(loop_scope="function")
async def test_unreadable_local_identity_refuses_before_record_or_output(
    scripted, monkeypatch, boot
):
    import psutil

    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    state = scripted
    monkeypatch.setattr(psutil, "boot_time", lambda: boot)
    with state.owner().bind(), pytest.raises(_WorkerSpawnAdmissionError):
        await collect(state)
    assert state.records == [] and not state.record_entered.is_set()
    assert state.cleanup_entered.is_set() and not state.first_io.is_set()


@pytest.mark.parametrize("probe", ["absent", "present", "permission-denied", "zombie-only"])
def test_cleanup_requires_kernel_absence_even_after_direct_child_exit(monkeypatch, probe):
    from lionagi.ln import _proc
    from lionagi.providers import _cli_subprocess as cs

    # Recorded rather than asserted inside the double: the probe's caller reads any
    # exception as an unanswered probe, so an assertion raised here would be swallowed
    # and every case but "absent" would still pass.
    calls: list[tuple[int, int]] = []

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        if probe == "absent":
            raise ProcessLookupError
        if probe == "permission-denied":
            raise PermissionError

    monkeypatch.setattr(_proc.os, "killpg", killpg, raising=False)
    child = SimpleNamespace(pid=12345, returncode=0)
    result = cs._group_cleanup_outcome(child, cs.SpawnedProcess(12345, 12345, 1.0))
    assert (result is cs._ChildGroupCleanup.CONFIRMED_EMPTY) is (probe == "absent")
    assert calls == [(12345, 0)]


def test_group_absence_does_not_replace_a_nonlive_owned_child(monkeypatch):
    from lionagi.providers import _cli_subprocess as cs

    monkeypatch.setattr(cs, "_group_has_members", lambda _pgid: False)
    child = SimpleNamespace(pid=12345, returncode=None)
    result = cs._group_cleanup_outcome(child, cs.SpawnedProcess(12345, 12345, 1.0))
    assert result is cs._ChildGroupCleanup.UNCONFIRMED


@pytest.mark.parametrize("reported", ["unconfirmed", "nonraising-none"])
@pytest.mark.skipif(sys.platform == "win32", reason="configured subagent caps require POSIX flock")
@pytest.mark.asyncio(loop_scope="function")
async def test_an_unconfirmed_dead_child_keeps_actual_holder_locks_until_explicit_fixture_cleanup(
    tmp_path, monkeypatch, reported
):
    from lionagi import _paths
    from lionagi.providers import _cli_subprocess as cs
    from lionagi.providers import _subagent_admission as admission

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "isolated-home")
    env = {"LIONAGI_MAX_SUBAGENTS": "1", "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS": "0"}
    lease = await admission.admit_subagent(engine=None, environ=env)
    replacement = None
    lease.record_child(999999, 1.0)
    real_process_create_time = admission.process_create_time
    monkeypatch.setattr(
        admission,
        "process_create_time",
        lambda pid: ("gone", None) if pid == 999999 else real_process_create_time(pid),
    )
    try:
        outcome = None if reported == "nonraising-none" else cs._ChildGroupCleanup.UNCONFIRMED
        cs._finish_child_lease(lease, outcome)
        refused = None
        try:
            replacement = await admission.admit_subagent(engine=None, environ=env)
        except admission.SubagentAdmissionRefusedError as exc:
            refused = exc
        assert refused is not None, "an unconfirmed group must block the next actual admission"
        assert lease.slots and all(slot.fd >= 0 for slot in lease.slots)
    finally:
        if replacement is not None:
            replacement.release()
        lease.release()


@pytest.mark.asyncio(loop_scope="function")
async def test_real_store_stale_generation_refuses_before_transport_and_leaves_records_unchanged(
    scripted, tmp_path
):
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError
    from lionagi.state.db import StateDB

    state = scripted
    async with StateDB(tmp_path / "worker-registration.db") as db:
        await db.create_progression("owner-progression")
        await db.create_session(
            {
                "id": "persisted-owner",
                "progression_id": "owner-progression",
                "status": "running",
                "invocation_kind": "agent",
                "cli_worker_generation": _GENERATION,
            }
        )
        owned = state.owner(db)
        await db.update_session("persisted-owner", cli_worker_generation=_OTHER_GENERATION)
        before = await db.snapshot_cli_worker_spawns("persisted-owner")
        with owned.bind(), pytest.raises(_WorkerSpawnAdmissionError) as raised:
            await collect(state)
        await owned.aclose()
        assert isinstance(raised.value.__cause__, ValueError)
        assert state.cleanup_entered.is_set() and not state.first_io.is_set()
        assert await db.snapshot_cli_worker_spawns("persisted-owner") == before


@pytest.mark.skipif(sys.platform == "win32", reason="own-session process identity requires POSIX")
@pytest.mark.asyncio(loop_scope="function")
async def test_real_python_child_commits_its_identity_before_callback_and_first_output(
    tmp_path, monkeypatch
):
    import math
    import uuid

    import psutil

    from lionagi import _paths
    from lionagi.providers import _cli_subprocess as cs
    from lionagi.state.cli_workers import _WorkerSpawnOwner
    from lionagi.state.db import StateDB

    admission_home = tmp_path / "admission-home"
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    monkeypatch.setattr(_paths, "LIONAGI_HOME", admission_home)
    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS", "1")
    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS_HEAVY", "1")
    monkeypatch.setenv("LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS", "0")
    session_id = "real-python-worker-owner"
    run_id = "real-python-worker-run"
    children = []
    callback_snapshots = []
    retained_before = set(cs._RETAINED_CLI_TASKS)
    program = (
        "import json, os, time; "
        "print(json.dumps({'ready': True, 'pid': os.getpid()}), flush=True); "
        "time.sleep(30)"
    )

    db = StateDB(tmp_path / "real-worker-registration.db")
    try:
        await asyncio.wait_for(db.open(), 10.0)
        await db.create_progression("real-python-worker-progression")
        await db.create_session(
            {
                "id": session_id,
                "progression_id": "real-python-worker-progression",
                "status": "running",
                "invocation_kind": "agent",
                "cli_worker_generation": _GENERATION,
            }
        )
        owner = _WorkerSpawnOwner(
            db, session_id=session_id, generation_id=_GENERATION, run_id=run_id
        )

        def track_child(proc, spawned):
            children.append((proc, spawned))

        async def on_spawn(spawned):
            snapshot = await db.snapshot_cli_worker_spawns(session_id)
            assert snapshot is not None and len(snapshot["spawns"]) == 1, (
                "the real worker identity must be durable before the public spawn callback"
            )
            row = snapshot["spawns"][0]
            assert (row["session_id"], row["generation_id"], row["run_id"]) == (
                session_id,
                _GENERATION,
                run_id,
            )
            assert row["pid"] == row["pgid"] == spawned.pid == spawned.pgid
            assert row["pid_create_time"] == spawned.create_time
            callback_snapshots.append(snapshot)

        with owner.bind(), cs._observe_run_cli_spawns(track_child):
            stream = cs.ndjson_from_cli(
                [sys.executable, "-c", program],
                cwd=child_home,
                env={"PATH": os.defpath, "HOME": str(child_home)},
                on_spawn=on_spawn,
            )
            try:
                item = await asyncio.wait_for(stream.__anext__(), 10.0)
                assert len(children) == len(callback_snapshots) == 1
                proc, spawned = children[0]
                assert item == {"ready": True, "pid": proc.pid}
                assert proc.returncode is None
                assert os.getpgid(proc.pid) == proc.pid == spawned.pgid
                snapshot = await db.snapshot_cli_worker_spawns(session_id)
                assert snapshot == callback_snapshots[0]
                assert snapshot["session"] == {
                    "id": session_id,
                    "status": "running",
                    "cli_worker_generation": _GENERATION,
                    "node_metadata": None,
                    "artifacts_path": None,
                    "source_kind": "live",
                }
                row = snapshot["spawns"][0]
                assert set(row) == {
                    "spawn_id",
                    "session_id",
                    "generation_id",
                    "run_id",
                    "engine_run_id",
                    "branch_id",
                    "pid",
                    "pgid",
                    "pid_create_time",
                    "pid_host",
                    "pid_boot_time",
                    "process_identity_mode",
                    "recorded_at",
                    "confirmed_empty_at",
                }, "durable worker records must contain identity fields, not argv, env or markers"
                assert str(uuid.UUID(row["spawn_id"])) == row["spawn_id"]
                assert row["engine_run_id"] is row["branch_id"] is None
                assert row["confirmed_empty_at"] is None
                assert row["pid"] == row["pgid"] == proc.pid > 1
                assert row["pid_host"] == socket.gethostname() and row["pid_host"].strip()
                assert row["pid_boot_time"] == psutil.boot_time()
                assert row["process_identity_mode"] == "local"
                for field in ("pid_create_time", "pid_boot_time", "recorded_at"):
                    assert type(row[field]) in (int, float) and math.isfinite(row[field])
            finally:
                try:
                    await asyncio.wait_for(stream.aclose(), 10.0)
                finally:
                    try:
                        for proc, _spawned in children:
                            if proc.returncode is None:
                                with contextlib.suppress(ProcessLookupError):
                                    proc.kill()
                            await asyncio.wait_for(proc.wait(), 5.0)
                    finally:
                        await asyncio.wait_for(owner.aclose(), 5.0)

        assert all(proc.returncode is not None for proc, _spawned in children)
        assert not (set(cs._RETAINED_CLI_TASKS) - retained_before)
        final = await db.snapshot_cli_worker_spawns(session_id)
        assert final == callback_snapshots[0], (
            "ending the real child and draining its owner must not settle or terminalize its record"
        )
    finally:
        await asyncio.wait_for(db.close(), 5.0)
