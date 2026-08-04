# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A caller that must record the process identity of a CLI leg it did not spawn
itself reads that identity through ``ndjson_from_cli``'s ``on_spawn`` callback,
and supplies the leg's environment through ``env``.

These run real subprocesses. The identity being tested is the kernel's, and a
mocked ``create_subprocess_exec`` would let a wrong pgid read pass: the whole
point of reading the identity at spawn is that the answer stops being available
once the child is reaped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from types import MappingProxyType, SimpleNamespace

import pytest

from lionagi.providers._cli_subprocess import (
    SpawnedProcess,
    ndjson_from_cli,
    spawned_create_time,
    spawned_pgid,
)

# Emits one NDJSON object, then blocks until killed. Long enough that the
# parent is still holding a live child when it inspects the group.
_SLEEPER = (
    "import sys, time; "
    'sys.stdout.write(\'{"type": "hello"}\\n\'); sys.stdout.flush(); '
    "time.sleep(30)"
)

# Emits one object and exits immediately, so by the time the stream drains the
# child is reaped and its group is no longer readable from its pid.
_QUICK = 'import sys; sys.stdout.write(\'{"type": "hello"}\\n\')'

# Produces nothing at all for a while. A callback that has fired against this
# child cannot have been reached by way of the first yielded object, because
# there is not going to be one.
_MUTE = "import time; time.sleep(30)"

# Ignores SIGTERM, then reports that it has, so a parent can be sure the
# handler is installed before it sends one. A SIGTERM racing the handler would
# kill this child outright and let the escalation path pass untested.
_IGNORES_TERM = (
    "import signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open(sys.argv[1], 'w').close(); "
    "time.sleep(30)"
)

# Prints the two variables the environment tests care about, as NDJSON.
_ECHO_ENV = (
    "import json, os, sys; "
    "sys.stdout.write(json.dumps({'type': 'env', "
    "'inherited': os.environ.get('LIONAGI_TEST_INHERITED'), "
    "'supplied': os.environ.get('LIONAGI_TEST_SUPPLIED')}) + '\\n')"
)

# How long a child ended by the failure path may take to actually die. Well
# under the sleepers' 30s, so expiry means teardown did not run rather than
# meaning the child finished by itself.
_TEARDOWN_DEADLINE = 15.0

_REQUEST_MODELS = [
    "lionagi.providers.anthropic.claude_code:ClaudeCodeRequest",
    "lionagi.providers.openai.codex:CodexCodeRequest",
]

# The descendant these tests are actually about. It ignores SIGTERM, writes its
# own pid where the test can read it, and then outlives anything polite.
_STUBBORN_DESCENDANT = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)

# A CLI parent that starts that descendant inside its own group and then reports
# ready. The parent itself dies promptly on SIGTERM, which is the shape that
# matters: waiting the process a handle points at is not draining the group it
# leads, so a teardown that equates the two leaves the descendant running.
_PARENT_WITH_STUBBORN_CHILD = (
    "import os, subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
    "while not os.path.exists(sys.argv[2]):\n"
    "    time.sleep(0.02)\n"
    "open(sys.argv[3], 'w').close()\n"
    "time.sleep(60)\n"
)


def _stubborn_cmd(kid_pid_file, ready_file) -> list[str]:
    return [
        sys.executable,
        "-c",
        _PARENT_WITH_STUBBORN_CHILD,
        _STUBBORN_DESCENDANT,
        str(kid_pid_file),
        str(ready_file),
    ]


async def _wait_for_file(path, limit: float = 15.0) -> bool:
    loop = asyncio.get_running_loop()
    until = loop.time() + limit
    while loop.time() < until:
        if path.exists():
            return True
        await asyncio.sleep(0.05)
    return False


def _is_alive(pid: int) -> bool:
    """Alive and not a corpse. A zombie answers signal 0 and is not running."""
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


async def _assert_dies(pid: int, what: str, deadline: float = _TEARDOWN_DEADLINE) -> None:
    """Fail unless *pid* is gone within the deadline, and never leave it running.

    Separate from :func:`_await_death` on two counts, both about a pid this
    process did not spawn. It is not our child, so nothing here reaps it and it
    can sit as a zombie that ``os.kill(pid, 0)`` answers for; status is what
    distinguishes a corpse from a runner. And it ignores SIGTERM and sleeps for
    a minute, so a failing assertion that merely reported would leave it behind
    for every later test in the session to trip over.
    """
    loop = asyncio.get_running_loop()
    until = loop.time() + deadline
    while loop.time() < until:
        if not _is_alive(pid):
            return
        await asyncio.sleep(0.05)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    pytest.fail(f"{what} ({pid}) outlived the teardown by {deadline}s")


def _cmd(script: str) -> list[str]:
    return [sys.executable, "-c", script]


async def _drain(stream) -> list[dict]:
    return [obj async for obj in stream]


async def _await_death(pid: int, deadline: float = _TEARDOWN_DEADLINE) -> None:
    """Fail unless *pid* is gone within *deadline* seconds.

    The deadline is the assertion. Waiting without one would also pass against
    an implementation that merely stopped reading and let a 30s sleeper reach
    its own end, which is the state this whole path exists to prevent.
    """
    loop = asyncio.get_running_loop()
    until = loop.time() + deadline
    while loop.time() < until:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"child {pid} was still alive {deadline}s after the callback failed")


def _scan_can_see_an_occupied_group() -> bool:
    """Whether the post-reap escalation can fire in this environment at all.

    Not a judgement about the platform, and not a check that the scan "works":
    it asks the enumerator the production code uses about a group that is
    provably occupied, because this process is in it. A scan that cannot find
    us in our own group cannot find a descendant in a child's either.

    It keys on membership alone and deliberately not on the completeness flag,
    because that is the order the decision itself uses: members present
    short-circuits ahead of any completeness question, and an incomplete scan
    that still saw a member kills the group. Gating on completeness would skip
    runs the code would have handled.
    """
    from lionagi.ln._proc import group_member_pids

    members, _ = group_member_pids(os.getpgrp())
    return os.getpid() in members


_needs_the_membership_scan = pytest.mark.skipif(
    not _scan_can_see_an_occupied_group(),
    reason=(
        "the membership scan cannot see this process in its own group, so it cannot "
        "see a descendant in a child's group either. Once the direct child is reaped "
        "the documented behaviour is to refuse to signal an unproven group, so a "
        "surviving descendant here is that refusal and not a defect"
    ),
)


class TestSpawnObservation:
    @pytest.mark.asyncio
    async def test_reports_the_live_child_pid_and_its_real_process_group(self):
        seen: list[SpawnedProcess] = []
        first_object_before_callback = []

        async def run():
            async for obj in ndjson_from_cli(_cmd(_SLEEPER), on_spawn=seen.append):
                first_object_before_callback.append(obj)
                # The child is still alive here, so the kernel can still be
                # asked what the callback claimed.
                assert os.getpgid(seen[0].pid) == seen[0].pgid
                break

        task = asyncio.create_task(run())
        await asyncio.wait_for(task, timeout=30)

        assert len(seen) == 1
        spawned = seen[0]
        # start_new_session=True: the child leads its own group, so the group
        # is the child's own pid and never this test process's.
        assert spawned.pgid == spawned.pid
        assert spawned.pgid != os.getpgid(0)
        assert first_object_before_callback == [{"type": "hello"}]

    @pytest.mark.asyncio
    async def test_the_callback_fires_on_a_child_that_has_produced_nothing(self):
        """The ordering IS the contract, and "before the first object" is too
        weak to state it: an implementation that recorded just before yielding
        would satisfy that and still leave a leg unrecorded for as long as it
        stayed quiet. This child never speaks, so a recording made here was
        made against the spawn and nothing else."""
        seen: list[SpawnedProcess] = []
        objects: list[dict] = []

        async def run():
            async for obj in ndjson_from_cli(_cmd(_MUTE), on_spawn=seen.append):
                objects.append(obj)  # pragma: no cover - the child is mute

        task = asyncio.create_task(run())
        try:
            # Generous next to a spawn, tiny next to the child's 30s silence.
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen, "the callback had not fired while the child sat silent"
            assert objects == []
        finally:
            task.cancel()
            with pytest.raises((asyncio.CancelledError, ProcessLookupError)):
                await task

    @pytest.mark.asyncio
    async def test_the_recorded_identity_is_bound_to_a_start_time(self):
        """pid and pgid are both recyclable, so on their own they name whatever
        the kernel has handed those numbers to by the time anyone looks. The
        start time is what makes them refer to this child, and it is readable
        only while the child exists, which is why this is not left to the
        consumer."""
        seen: list[SpawnedProcess] = []
        live: list[float | None] = []

        async def run():
            async for _ in ndjson_from_cli(_cmd(_SLEEPER), on_spawn=seen.append):
                # Read here, where the child is provably alive. Breaking out of
                # this loop closes the generator and ends the child, so the same
                # read after the loop races that teardown and returns None for a
                # child that has since been killed — which says nothing about
                # what was recorded.
                live.append(spawned_create_time(seen[0].pid))
                break

        task = asyncio.create_task(run())
        await asyncio.wait_for(task, timeout=30)

        assert len(seen) == 1
        spawned = seen[0]
        assert spawned.create_time is not None
        # The value is this child's own, not a constant and not the parent's.
        assert spawned.create_time != spawned_create_time(os.getpid())
        assert live == [spawned.create_time]

    @pytest.mark.asyncio
    async def test_the_identity_is_read_as_one_observation(self):
        """Group and start time are two reads by pid, and a pid reassigned
        between them answers the second as the replacement process. A record
        mixing the two would describe a process that never existed, so the
        group read is bracketed by the start time and a bracket that fails
        yields no start time at all."""
        import lionagi.providers._cli_subprocess as mod

        real = mod.spawned_create_time
        answers = iter([100.0, 200.0])

        def shifting(pid: int) -> float | None:
            return next(answers, 200.0)

        monkey = pytest.MonkeyPatch()
        monkey.setattr(mod, "spawned_create_time", shifting)
        try:
            mixed = mod.observe_spawned(os.getpid())
        finally:
            monkey.undo()

        assert mixed.pid == os.getpid()
        assert mixed.create_time is None, "a moved start time must not be recorded as an identity"
        # The same call against a stable process does record one.
        assert mod.observe_spawned(os.getpid()).create_time == real(os.getpid())

    @pytest.mark.asyncio
    async def test_identity_is_recorded_even_for_a_child_that_exits_at_once(self):
        """A read deferred to teardown cannot answer for a reaped child, so the
        observation has to happen before anything is streamed — which is what
        this asserts, by ordering rather than by content.

        What it deliberately does NOT assert is a start time. This child can be
        reaped before the read reaches it, and no one can recover the start time
        of a reaped process; requiring one here produced a test that passes
        alone and fails under the load of the full suite. The claim that a live
        child's identity IS bound to its start time belongs to the test above,
        which holds the child open and can therefore make it. Here the only
        thing that must hold is that an absent start time is reported as absent
        and never filled in from somewhere else."""
        seen: list[SpawnedProcess] = []
        order: list[str] = []
        objs: list[dict] = []

        def record(spawned: SpawnedProcess) -> None:
            order.append("spawn")
            seen.append(spawned)

        async for obj in ndjson_from_cli(_cmd(_QUICK), on_spawn=record):
            order.append("output")
            objs.append(obj)

        assert objs == [{"type": "hello"}]
        assert len(seen) == 1
        assert seen[0].pgid == seen[0].pid
        # The observation preceded the stream. A read moved to teardown fails
        # this, which is the defect the early read exists to avoid.
        assert order[0] == "spawn"
        if seen[0].create_time is not None:
            assert seen[0].create_time != spawned_create_time(os.getpid())

    @pytest.mark.asyncio
    async def test_an_async_recorder_is_awaited_before_the_stream_is_read(self):
        """A durable recorder is written in the runner's own async style, and
        ``Callable[..., None]`` does not reject an ``async def`` at runtime: an
        un-awaited one would return a coroutine that is quietly dropped, so the
        leg runs entirely unrecorded and nothing raises. The recording finishes
        before the first object arrives, so a consumer that acts on the stream
        can never be ahead of the record.

        Asserting that only after the stream has drained would pass against an
        implementation that started reading first and awaited the recorder at
        the end, so the state is sampled at the first object instead, and the
        recorder is made slow next to the time a child takes to emit one line
        rather than relying on scheduling luck to separate the two.
        """
        recorded: list[SpawnedProcess] = []
        recorded_at_first_object: list[bool] = []

        async def recorder(spawned: SpawnedProcess) -> None:
            await asyncio.sleep(0.25)
            recorded.append(spawned)

        objs = []
        async for obj in ndjson_from_cli(_cmd(_QUICK), on_spawn=recorder):
            recorded_at_first_object.append(bool(recorded))
            objs.append(obj)

        assert objs == [{"type": "hello"}]
        assert len(recorded) == 1
        assert recorded[0].pid > 0
        assert recorded_at_first_object == [True]

    @pytest.mark.asyncio
    async def test_no_callback_is_the_unchanged_path(self):
        assert await _drain(ndjson_from_cli(_cmd(_QUICK))) == [{"type": "hello"}]


class TestSpawnObservationFailure:
    @pytest.mark.asyncio
    async def test_a_failing_recorder_propagates_and_leaves_no_running_child(self):
        """Swallowing the recorder's failure would leave a live leg outside
        whatever domain the record defines — precisely the process nobody can
        later sweep. So it propagates, and the child it was called for is
        ended on the way out."""
        seen: list[int] = []

        class RecorderFailed(RuntimeError):
            pass

        def boom(spawned: SpawnedProcess) -> None:
            seen.append(spawned.pid)
            raise RecorderFailed("cannot record")

        with pytest.raises(RecorderFailed):
            await _drain(ndjson_from_cli(_cmd(_SLEEPER), on_spawn=boom))

        assert len(seen) == 1
        await _await_death(seen[0])

    @pytest.mark.asyncio
    async def test_a_cancelled_recorder_also_leaves_no_running_child(self):
        """``CancelledError`` and ``KeyboardInterrupt`` do not derive from
        ``Exception``, so a guard written against that base would let both pass
        while the child it had just created kept running. Cancellation is the
        realistic one: a runner shutting down mid-spawn is exactly when an
        unrecorded survivor is least likely to be noticed."""
        seen: list[int] = []

        def cancel(spawned: SpawnedProcess) -> None:
            seen.append(spawned.pid)
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await _drain(ndjson_from_cli(_cmd(_SLEEPER), on_spawn=cancel))

        assert len(seen) == 1
        await _await_death(seen[0])

    @pytest.mark.asyncio
    async def test_a_second_cancellation_during_teardown_still_ends_the_child(self, tmp_path):
        """The graceful teardown sends SIGTERM and then waits out a grace before
        escalating, and that wait is a cancellation point. A runner being torn
        down is where a second cancellation actually arrives, and a child that
        ignores SIGTERM would then outlive the escalation that never ran, with
        nobody holding a record of it. So the escalation does not depend on the
        wait surviving."""
        ready = tmp_path / "ignoring-sigterm"
        seen: list[int] = []

        async def boom(spawned: SpawnedProcess) -> None:
            seen.append(spawned.pid)
            for _ in range(200):
                if ready.exists():
                    break
                await asyncio.sleep(0.05)
            assert ready.exists(), "the child never reported that it ignores SIGTERM"
            raise RuntimeError("cannot record")

        task = asyncio.create_task(
            _drain(
                ndjson_from_cli(
                    [sys.executable, "-c", _IGNORES_TERM, str(ready)],
                    on_spawn=boom,
                )
            )
        )
        for _ in range(200):
            if seen and ready.exists():
                break
            await asyncio.sleep(0.05)
        assert seen, "the recorder never fired"

        # Long enough to be inside the grace wait, far short of the grace
        # itself, so this cancels a wait that is genuinely in progress.
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await task

        await _await_death(seen[0])

    @pytest.mark.asyncio
    async def test_a_failing_async_recorder_is_treated_the_same(self):
        seen: list[int] = []

        class RecorderFailed(RuntimeError):
            pass

        async def boom(spawned: SpawnedProcess) -> None:
            seen.append(spawned.pid)
            raise RecorderFailed("cannot record")

        with pytest.raises(RecorderFailed):
            await _drain(ndjson_from_cli(_cmd(_SLEEPER), on_spawn=boom))

        assert len(seen) == 1
        await _await_death(seen[0])


class TestSpawnedPgid:
    def test_falls_back_to_the_pid_when_the_group_cannot_be_read(self):
        """Every spawn here uses start_new_session, so a child leads its own
        group and its pid IS the group id. The fallback is exact rather than a
        guess, which is why an unreadable group is not an error."""
        # A pid that cannot be resolved: 0 is this process's group by
        # definition, so use a pid this process may not query.
        dead = _a_reaped_pid()
        assert spawned_pgid(dead) == dead

    def test_reads_the_real_group_of_a_live_process(self):
        assert spawned_pgid(os.getpid()) == os.getpgid(os.getpid())


class TestSpawnedCreateTime:
    def test_reads_a_live_process_start_time(self):
        assert spawned_create_time(os.getpid()) is not None

    def test_a_reaped_pid_yields_no_start_time_rather_than_a_wrong_one(self):
        """None here has to mean "nothing was established". Returning any
        number for a pid whose process is gone would hand a consumer a binding
        that matches whatever the OS next puts at that pid."""
        assert spawned_create_time(_a_reaped_pid()) is None


def _a_reaped_pid() -> int:
    """Spawn a trivial child, wait for it, and return its now-reaped pid."""
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", ""])  # noqa: S603 - fixed argv
    proc.wait()
    return proc.pid


class TestLegEnvironment:
    @pytest.mark.asyncio
    async def test_none_inherits_this_process_environment(self, monkeypatch):
        monkeypatch.setenv("LIONAGI_TEST_INHERITED", "from-parent")

        objs = await _drain(ndjson_from_cli(_cmd(_ECHO_ENV)))

        assert objs[0]["inherited"] == "from-parent"

    @pytest.mark.asyncio
    async def test_a_supplied_mapping_replaces_rather_than_merges(self, monkeypatch):
        """The replace-wholesale contract is stated on the field because the
        alternative reading (merge) is the one a caller assumes, and a caller
        who assumes it ships a leg missing PATH."""
        monkeypatch.setenv("LIONAGI_TEST_INHERITED", "from-parent")

        objs = await _drain(
            ndjson_from_cli(
                _cmd(_ECHO_ENV),
                env={"LIONAGI_TEST_SUPPLIED": "from-caller", "PATH": os.environ.get("PATH", "")},
            )
        )

        assert objs[0]["supplied"] == "from-caller"
        assert objs[0]["inherited"] is None


class TestRequestModelsCarryThem:
    """The runner sets these on a request object; nothing renders them as CLI
    arguments and nothing serialises them into a persisted request."""

    def test_claude_code_request_keeps_them_off_the_wire(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        req = ClaudeCodeRequest(prompt="hi", env={"A": "1"}, on_spawn=lambda spawned: None)

        assert req.env == {"A": "1"}
        assert callable(req.on_spawn)
        dumped = req.model_dump()
        assert "env" not in dumped and "on_spawn" not in dumped
        assert req.as_cmd_args() == ClaudeCodeRequest(prompt="hi").as_cmd_args()

    def test_codex_request_keeps_them_off_the_wire(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", env={"A": "1"}, on_spawn=lambda spawned: None)

        assert req.env == {"A": "1"}
        assert callable(req.on_spawn)
        dumped = req.model_dump()
        assert "env" not in dumped and "on_spawn" not in dumped
        assert req.as_cmd_args() == CodexCodeRequest(prompt="hi").as_cmd_args()

    def test_the_argv_baseline_is_a_real_comparison(self):
        """The two tests above assert an EXACT argv match against a request
        without the runtime fields, rather than scanning for suspicious-looking
        tokens. A scan passes for anything whose spelling the scan did not
        anticipate, including the environment's own values. This asserts the
        baseline is not vacuously equal to everything."""
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        baseline = ClaudeCodeRequest(prompt="hi").as_cmd_args()
        assert baseline != ClaudeCodeRequest(prompt="hi", model="opus").as_cmd_args()

    def test_neither_field_is_rendered_by_repr(self):
        """``exclude=True`` governs serialisation only. A complete child
        environment normally carries credentials, and pydantic's default repr
        would print the whole mapping into any log line, f-string, or exception
        that happens to include the request."""
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
        from lionagi.providers.openai.codex import CodexCodeRequest

        secret = "leaked-credential-value"
        for model in (ClaudeCodeRequest, CodexCodeRequest):
            req = model(prompt="hi", env={"TOKEN": secret}, on_spawn=lambda spawned: None)
            assert secret not in repr(req)
            assert secret not in str(req)
            assert "on_spawn" not in repr(req)
            # The value is still there to be used; it is only unprinted.
            assert req.env == {"TOKEN": secret}

    def test_a_malformed_environment_is_rejected_without_printing_any_value(self):
        """The error a bad environment raises is itself a disclosure channel:
        pydantic quotes the entire rejected input into a ValidationError, so a
        single wrongly typed entry would print every credential beside it."""
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
        from lionagi.providers.openai.codex import CodexCodeRequest

        secret = "leaked-credential-value"
        for model in (ClaudeCodeRequest, CodexCodeRequest):
            with pytest.raises(TypeError) as excinfo:
                model(prompt="hi", env={"TOKEN": secret, "BROKEN": 1})
            message = str(excinfo.value)
            assert "BROKEN" in message
            assert secret not in message
            assert "TOKEN" not in message

    def test_both_default_to_absent(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
        from lionagi.providers.openai.codex import CodexCodeRequest

        for req in (ClaudeCodeRequest(prompt="hi"), CodexCodeRequest(prompt="hi")):
            assert req.env is None
            assert req.on_spawn is None

    def test_the_request_models_still_generate_a_json_schema(self):
        """`exclude=True` keeps a field out of a serialised INSTANCE; it does
        nothing about the model's schema, which is generated from the class and
        walks every field. Endpoint config asks for exactly this schema when it
        serialises `request_options`, and a callable has no JSON schema at all
        — so a plain Callable field here fails every code path that persists a
        CLI request, none of which is in this module."""
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
        from lionagi.providers.openai.codex import CodexCodeRequest

        for model in (ClaudeCodeRequest, CodexCodeRequest):
            schema = model.model_json_schema()
            properties = schema.get("properties", {})
            assert "on_spawn" not in properties
            assert "env" not in properties

    @pytest.mark.asyncio
    async def test_claude_code_stream_hands_both_to_the_spawn_helper(self, monkeypatch):
        import lionagi.providers.anthropic.claude_code as cc

        captured: dict = {}

        async def fake_ndjson(cmd, **kwargs):
            captured.update(kwargs)
            captured["cmd"] = cmd
            return
            yield  # pragma: no cover - generator shape only

        monkeypatch.setattr(cc, "ndjson_from_cli", fake_ndjson)
        monkeypatch.setattr(cc, "CLAUDE_CLI", "claude")

        recorder = lambda spawned: None  # noqa: E731 - identity is what is asserted
        req = cc.ClaudeCodeRequest(prompt="hi", env={"A": "1"}, on_spawn=recorder)
        await _drain(cc.stream_cc_cli_events(req))

        assert captured["env"] == {"A": "1"}
        assert captured["on_spawn"] is recorder

    @pytest.mark.asyncio
    async def test_codex_stream_hands_both_to_the_spawn_helper(self, monkeypatch):
        import lionagi.providers.openai.codex as cx

        captured: dict = {}

        async def fake_ndjson(cmd, **kwargs):
            captured.update(kwargs)
            captured["cmd"] = cmd
            return
            yield  # pragma: no cover - generator shape only

        monkeypatch.setattr(cx, "ndjson_from_cli", fake_ndjson)
        monkeypatch.setattr(cx, "CODEX_CLI", "codex")

        recorder = lambda spawned: None  # noqa: E731 - identity is what is asserted
        req = cx.CodexCodeRequest(prompt="hi", env={"A": "1"}, on_spawn=recorder)
        await _drain(cx.stream_codex_cli_events(req))

        assert captured["env"] == {"A": "1"}
        assert captured["on_spawn"] is recorder


class TestEndpointsPreserveRuntimeState:
    """``create_payload`` rebuilds the request from its dump, and the dump omits
    both fields by construction. Without an explicit carry, a caller who passes
    the advertised request model to an endpoint gets a leg with the inherited
    environment and no spawn record, silently and with nothing raised."""

    @pytest.mark.parametrize(
        "endpoint_path,request_path",
        [
            (
                "lionagi.providers.anthropic.claude_code:ClaudeCodeCLIEndpoint",
                "lionagi.providers.anthropic.claude_code:ClaudeCodeRequest",
            ),
            (
                "lionagi.providers.openai.codex:CodexCLIEndpoint",
                "lionagi.providers.openai.codex:CodexCodeRequest",
            ),
        ],
    )
    def test_a_populated_request_survives_create_payload(self, endpoint_path, request_path):
        endpoint_cls = _load(endpoint_path)
        request_cls = _load(request_path)

        def recorder(spawned):
            return None

        payload, _ = endpoint_cls().create_payload(
            request_cls(prompt="hi", env={"A": "1"}, on_spawn=recorder)
        )

        rebuilt = payload["request"]
        assert rebuilt is not None
        assert rebuilt.env == {"A": "1"}
        assert rebuilt.on_spawn is recorder

    @pytest.mark.parametrize(
        "endpoint_path,request_path",
        [
            (
                "lionagi.providers.anthropic.claude_code:ClaudeCodeCLIEndpoint",
                "lionagi.providers.anthropic.claude_code:ClaudeCodeRequest",
            ),
            (
                "lionagi.providers.openai.codex:CodexCLIEndpoint",
                "lionagi.providers.openai.codex:CodexCodeRequest",
            ),
        ],
    )
    def test_an_explicit_keyword_still_overrides_the_carried_value(
        self, endpoint_path, request_path
    ):
        """The carry fills a gap the rebuild opened; it does not outrank what
        the caller asked for at the call site. Asserted for both fields: they
        are carried by one loop, and a mutant that reversed the precedence for
        only one of them would go unseen by a test that watched only ``env``.
        """
        endpoint_cls = _load(endpoint_path)
        request_cls = _load(request_path)

        def on_request(spawned):
            return None

        def at_call_site(spawned):
            return None

        payload, _ = endpoint_cls().create_payload(
            request_cls(prompt="hi", env={"A": "1"}, on_spawn=on_request),
            env={"B": "2"},
            on_spawn=at_call_site,
        )

        assert payload["request"].env == {"B": "2"}
        assert payload["request"].on_spawn is at_call_site

    @pytest.mark.parametrize(
        "endpoint_path",
        [
            "lionagi.providers.anthropic.claude_code:ClaudeCodeCLIEndpoint",
            "lionagi.providers.openai.codex:CodexCLIEndpoint",
        ],
    )
    def test_a_request_passed_as_a_dict_is_unaffected(self, endpoint_path):
        """A dict never went through a dump, so its values were never at risk.
        This is the canonical iModel route and it must keep behaving as it did.
        """
        endpoint_cls = _load(endpoint_path)

        def recorder(spawned):
            return None

        payload, _ = endpoint_cls().create_payload(
            {"prompt": "hi", "env": {"A": "1"}, "on_spawn": recorder}
        )

        assert payload["request"].env == {"A": "1"}
        assert payload["request"].on_spawn is recorder


class TestTheConfigRouteKeepsThemOutOfWhatIsSerialised:
    """``iModel(**kwargs)`` forwards anything it does not recognise into
    ``EndpointConfig.kwargs``, which is a supported way to configure an endpoint
    and also exactly what ``Endpoint.to_dict`` serialises — so it reaches
    ``iModel.to_dict``, ``Branch.to_dict``, and the run snapshots written to
    disk. A child environment left there is a credential in a saved file."""

    SECRET = "leaked-credential-value"

    @pytest.mark.parametrize("provider", ["claude_code", "codex"])
    def test_the_environment_does_not_reach_a_serialised_model(self, provider):
        from lionagi.service.imodel import iModel

        imodel = iModel(provider=provider, env={"TOKEN": self.SECRET})

        assert self.SECRET not in json.dumps(imodel.to_dict())

    @pytest.mark.parametrize("provider", ["claude_code", "codex"])
    def test_a_callback_does_not_break_the_snapshot_it_would_be_written_into(self, provider):
        """A function has no JSON form. Left in the serialised config it does
        not merely leak, it raises when anything tries to persist the branch,
        which is every run that saves its state."""
        from lionagi.service.imodel import iModel
        from lionagi.session.branch import Branch

        def recorder(spawned):
            return None

        branch = Branch(
            chat_model=iModel(provider=provider, env={"TOKEN": self.SECRET}, on_spawn=recorder)
        )

        dumped = json.dumps(branch.to_dict())
        assert self.SECRET not in dumped
        assert "on_spawn" not in dumped

    @pytest.mark.parametrize("provider", ["claude_code", "codex"])
    def test_the_configuration_route_still_reaches_the_request(self, provider):
        """Moving the values out of the serialised config must not move them
        out of the caller's reach: this is the route the runner actually uses.
        """
        from lionagi.service.imodel import iModel

        def recorder(spawned):
            return None

        imodel = iModel(provider=provider, env={"TOKEN": self.SECRET}, on_spawn=recorder)
        payload, _ = imodel.endpoint.create_payload({"prompt": "hi"})

        assert payload["request"].env == {"TOKEN": self.SECRET}
        assert payload["request"].on_spawn is recorder

    @pytest.mark.parametrize("provider", ["claude_code", "codex"])
    def test_a_copy_still_notifies_the_original_supervisor(self, provider):
        """``iModel.copy`` deep copies the config, and a deep copy of a bound
        callback copies its receiver too — so the copy's legs would report to a
        copied supervisor while the real one, which owns the durable process
        accounting, hears nothing and every wiring check still passes.

        It has to be a BOUND method. ``deepcopy`` of a plain function returns
        the same object, so a test written with a nested function stays green
        against an implementation that deep copies everything, which is the one
        thing it exists to catch.
        """
        from lionagi.service.imodel import iModel

        supervisor = _Supervisor()
        imodel = iModel(provider=provider, env={"TOKEN": self.SECRET}, on_spawn=supervisor.observe)
        clone = imodel.copy()

        payload, _ = clone.endpoint.create_payload({"prompt": "hi"})
        payload["request"].on_spawn("spawned")

        assert supervisor.seen == ["spawned"], "the copy reported to a different supervisor"
        assert payload["request"].env == {"TOKEN": self.SECRET}, "the copy lost the environment"
        assert self.SECRET not in json.dumps(clone.to_dict())

    @pytest.mark.parametrize(
        "endpoint_path",
        [
            "lionagi.providers.anthropic.claude_code:ClaudeCodeCLIEndpoint",
            "lionagi.providers.openai.codex:CodexCLIEndpoint",
        ],
    )
    def test_a_supplied_config_still_notifies_the_original_supervisor(self, endpoint_path):
        """The other construction route. ``Endpoint.__init__`` copies a supplied
        ``EndpointConfig`` with ``model_copy(deep=True)`` before anything of
        ours runs, so the runtime values have to be lifted off the caller's own
        object first or the endpoint holds the copy's rebound callback."""
        import copy as copy_module

        endpoint_cls = _load(endpoint_path)
        supervisor = _Supervisor()

        config = copy_module.deepcopy(endpoint_cls().config)
        config.kwargs["on_spawn"] = supervisor.observe
        config.kwargs["env"] = {"TOKEN": self.SECRET}

        endpoint = endpoint_cls(config=config)
        payload, _ = endpoint.create_payload({"prompt": "hi"})
        payload["request"].on_spawn("spawned")

        assert supervisor.seen == ["spawned"]
        assert payload["request"].env == {"TOKEN": self.SECRET}
        assert self.SECRET not in json.dumps(endpoint.to_dict())


class _Supervisor:
    """Stands in for the thing that owns durable process accounting.

    Its ``observe`` is a bound method precisely because that is what a deep
    copy rebinds; a module-level function would be copied to itself and prove
    nothing.
    """

    def __init__(self):
        self.seen: list[object] = []

    def observe(self, spawned):
        self.seen.append(spawned)


class TestTheEnvironmentSurvivesNoErrorPath:
    """``repr=False`` governs the model's own representation. It says nothing
    about the channel pydantic opens when a DIFFERENT validator fails: the
    input of a failing model-level validator is rendered verbatim, and at that
    point the input is the caller's whole raw mapping."""

    SECRET = "leaked-credential-value"

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_an_unrelated_validation_failure_prints_no_variable(self, model_path):
        model = _load(model_path)

        with pytest.raises(Exception) as excinfo:  # noqa: B017 - the type is the point below
            model(env={"TOKEN": self.SECRET})

        # Every channel pydantic renders this through, not just the one a
        # human reads. str() and errors() go via repr, json() walks the
        # structure and writes out keys and values, and json() is what a
        # structured logger emits — so a carrier that is quiet in repr and
        # still a mapping passes the first two and leaks through the third.
        err = excinfo.value
        rendered = {
            "str": str(err),
            "errors": repr(err.errors()),
            "json": err.json(),
        }
        for channel, text in rendered.items():
            assert self.SECRET not in text, f"the environment leaked through {channel}"
        # The request really was rejected, so the absence above is a redaction
        # and not a request that quietly succeeded.
        assert "env" not in rendered["str"] or "variable(s)" in rendered["str"]
        assert excinfo.type.__name__ == "ValidationError"

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_a_callback_receiver_is_not_printed_by_any_channel(self, model_path):
        """A bound method carries its receiver into its own repr, so the
        supervisor's attributes are rendered with it. The callback is a
        credential channel for the same reason the environment is."""
        model = _load(model_path)

        class Supervisor:
            """The repr is what makes this a channel. A default object repr
            shows an address and nothing else, so asserting against one proves
            nothing — and receivers that DO render their attributes are the
            common case here: dataclasses, pydantic models, anything holding
            the credentials it was configured with."""

            def __init__(self):
                self.token = "leaked-from-the-supervisor"

            def __repr__(self):
                return f"Supervisor(token={self.token!r})"

            def record(self, spawned):
                pass

        sup = Supervisor()

        with pytest.raises(Exception) as excinfo:  # noqa: B017 - see above
            model(on_spawn=sup.record)

        err = excinfo.value
        for channel, text in (
            ("str", str(err)),
            ("errors", repr(err.errors())),
            ("json", err.json()),
        ):
            assert sup.token not in text, f"the receiver leaked through {channel}"

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_a_redacted_callback_is_still_the_callable_the_caller_passed(self, model_path):
        """The redaction wraps the value before validation sees it, so the
        field validator has to unwrap it. Without that, pydantic rejects a
        perfectly good callback as not callable — a failure a leak test alone
        would never show."""
        model = _load(model_path)
        seen = []

        req = model(prompt="hi", on_spawn=seen.append)

        # Not ``is``: a bound method is a new object on every attribute access,
        # so identity fails here even when nothing was replaced. What has to
        # hold is that calling it reaches the caller's own object.
        req.on_spawn("recorded")
        assert seen == ["recorded"]

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_a_non_string_key_is_reported_by_position_not_printed(self, model_path):
        """A string key is a variable NAME and naming it is what makes the
        error actionable. A key of any other type is not a name, and printing
        it prints whatever the caller put in it."""
        model = _load(model_path)
        secret_key = (self.SECRET,)

        with pytest.raises(TypeError) as excinfo:
            model(prompt="hi", env={secret_key: "ok"})

        message = str(excinfo.value)
        assert self.SECRET not in message
        assert "entry 0" in message and "tuple" in message

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_a_string_key_is_still_named_because_a_name_is_not_a_secret(self, model_path):
        model = _load(model_path)

        with pytest.raises(TypeError) as excinfo:
            model(prompt="hi", env={"CARGO_TARGET_DIR": 3})

        assert "CARGO_TARGET_DIR" in str(excinfo.value)

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_an_environment_that_is_not_a_mapping_is_not_printed(self, model_path):
        """The redaction substitutes one mapping for another. A caller who
        passes a sequence of pairs — a plausible way to build an environment —
        goes past it untouched, so this rejection is the only thing standing
        between that value and an error that would quote the whole of it."""
        model = _load(model_path)

        with pytest.raises(TypeError) as excinfo:
            model(prompt="hi", env=[("TOKEN", self.SECRET)])

        message = str(excinfo.value)
        assert self.SECRET not in message
        assert "list" in message


class TestTheWholeGroupIsEnded:
    """A CLI leg is a group, not a process.

    Every spawn here uses ``start_new_session``, so the child leads a group that
    holds whatever it starts. Teardown that waits the handle it holds is waiting
    one member of that group; the rest are reparented and keep running, and
    nothing in the parent's own state says so. These use a descendant that
    ignores SIGTERM, because a cooperative one dies to the polite signal and
    makes every version of this path look identical.

    Both reach the descendant only *after* the graceful pass has waited the
    direct child, which is the one moment the code needs the membership scan:
    the leader's pid is recyclable from then on, and only a live member pins
    the group id. Where that scan cannot see a live group the documented
    outcome is a refusal to signal, so the descendant survives by design and
    these would report a defect that is not there. They are gated on a positive
    control rather than on a platform name, and the gate skips nothing about
    the refusal itself: what the decision does with an unreadable table is
    pinned unconditionally, against a forced answer, in
    :class:`TestWhatTheProcessTableCouldNotAnswer`.
    """

    @pytest.mark.asyncio
    @_needs_the_membership_scan
    async def test_a_failing_recorder_ends_the_descendants_too(self, tmp_path):
        kid_pid = tmp_path / "kid.pid"
        ready = tmp_path / "ready"

        async def boom(spawned: SpawnedProcess) -> None:
            assert await _wait_for_file(ready), "the leg never reported ready"
            raise RuntimeError("cannot record")

        with pytest.raises(RuntimeError):
            await _drain(ndjson_from_cli(_stubborn_cmd(kid_pid, ready), on_spawn=boom))

        await _assert_dies(int(kid_pid.read_text()), "the descendant of a failed spawn")

    @pytest.mark.asyncio
    @_needs_the_membership_scan
    async def test_a_cancelled_stream_ends_the_descendants_too(self, tmp_path):
        """The recorder succeeds here, so the record exists and the ordinary
        teardown runs. That is the reassuring case, and it is where the direct
        child dies promptly to SIGTERM, the wait it is being watched by returns,
        and the escalation that would have reached the rest of the group never
        fires."""
        kid_pid = tmp_path / "kid.pid"
        ready = tmp_path / "ready"
        recorded: list[SpawnedProcess] = []

        task = asyncio.create_task(
            _drain(ndjson_from_cli(_stubborn_cmd(kid_pid, ready), on_spawn=recorded.append))
        )
        assert await _wait_for_file(ready), "the leg never reported ready"
        assert recorded, "the recorder never fired"

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        await _assert_dies(int(kid_pid.read_text()), "the descendant of a cancelled stream")
        await _await_death(recorded[0].pid)


class TestACancellationBeforeTheHandleArrives:
    @pytest.mark.asyncio
    async def test_a_child_started_but_never_returned_is_still_ended(self, tmp_path, monkeypatch):
        """The OS has already started the child by the time
        ``create_subprocess_exec`` resumes. A cancellation landing in the window
        before it returns leaves a running leg that this process holds no handle
        for and that no callback has recorded, so neither the teardown below nor
        any later sweep over the records can reach it.

        The window is widened deliberately. It is real and short, and a test
        that raced it would pass on timing rather than on the shield.
        """
        kid_pid = tmp_path / "kid.pid"
        ready = tmp_path / "ready"
        started: list[int] = []
        recorded: list[SpawnedProcess] = []
        real = asyncio.create_subprocess_exec

        async def slow(*args, **kwargs):
            proc = await real(*args, **kwargs)
            started.append(proc.pid)
            await asyncio.sleep(2.0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)

        task = asyncio.create_task(
            _drain(ndjson_from_cli(_stubborn_cmd(kid_pid, ready), on_spawn=recorded.append))
        )
        assert await _wait_for_file(ready), "the leg never reported ready"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # The premise, asserted rather than assumed: the OS made the child and
        # nothing recorded it. Without both, this passes for the wrong reason.
        assert started, "the spawn never reached the OS"
        assert not recorded, "the cancellation did not land inside the spawn window"

        await _assert_dies(int(kid_pid.read_text()), "the descendant of an abandoned spawn")
        await _assert_dies(started[0], "the abandoned leg")


def _load(path: str):
    module_name, _, attr = path.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), attr)


class TestAnEndpointTheCallerAlreadyBuilt:
    """``iModel(endpoint=<instance>, ...)`` is a supported signature and it
    takes a branch that keeps the endpoint and discards every other keyword.
    For most keywords that only loses configuration the endpoint already has;
    for these two it hands the child a default environment and leaves the
    supervisor hearing nothing, with nothing raised and nothing logged. That
    reads exactly like a working leg."""

    ENDPOINTS = [
        "lionagi.providers.anthropic.claude_code:ClaudeCodeCLIEndpoint",
        "lionagi.providers.openai.codex:CodexCLIEndpoint",
    ]

    @pytest.mark.parametrize("endpoint_path", ENDPOINTS)
    def test_both_runtime_values_reach_the_request(self, endpoint_path):
        from lionagi.service.imodel import iModel

        endpoint_cls = _load(endpoint_path)
        seen: list = []

        model = iModel(endpoint=endpoint_cls(), env={"TOKEN": "supplied"}, on_spawn=seen.append)
        payload, _ = model.endpoint.create_payload({"prompt": "hi"})
        request = payload["request"]

        assert request.env == {"TOKEN": "supplied"}
        # Reached by calling it, because a bound method is a new object on every
        # access and identity would fail here even with nothing replaced.
        request.on_spawn("recorded")
        assert seen == ["recorded"]

    @pytest.mark.parametrize("endpoint_path", ENDPOINTS)
    def test_an_absent_value_does_not_erase_what_the_endpoint_was_built_with(self, endpoint_path):
        """``None`` is the absence of a value, not an instruction to clear one.
        A caller who passes an endpoint already carrying an environment and does
        not mention it again must keep it."""
        from lionagi.service.imodel import iModel

        endpoint_cls = _load(endpoint_path)
        endpoint = endpoint_cls(env={"TOKEN": "built-in"})

        model = iModel(endpoint=endpoint, env=None)
        payload, _ = model.endpoint.create_payload({"prompt": "hi"})

        assert payload["request"].env == {"TOKEN": "built-in"}

    def test_an_endpoint_with_nowhere_to_put_them_refuses(self):
        """The alternative to placing them is refusing them. Dropping them is
        what this whole class exists to rule out, and a non-CLI endpoint has no
        child process for either value to describe."""
        from lionagi.service.connections.match_endpoint import match_endpoint
        from lionagi.service.imodel import iModel

        endpoint = match_endpoint(provider="openai", endpoint="chat")

        with pytest.raises(TypeError) as excinfo:
            iModel(endpoint=endpoint, env={"TOKEN": "nowhere"})

        assert "env" in str(excinfo.value)


class TestWhatTheProcessTableCouldNotAnswer:
    """The escalation keys on positive evidence that the recorded group id is
    still this child's, and an unreadable process table produces no evidence
    either way. That branch cannot be reached by running real processes on a
    machine where enumeration works, so the enumerator is stubbed: the point
    under test is what the decision does with each answer, not how the answer
    was obtained."""

    def _decide(self, monkeypatch, members, complete):
        import lionagi.providers._cli_subprocess as cs

        killed: list = []
        monkeypatch.setattr(cs, "group_member_pids", lambda pgid: (members, complete))
        monkeypatch.setattr(cs, "kill_group_now", lambda pgid: killed.append(pgid))
        return cs._kill_group_if_occupied(4242), killed

    def test_a_group_holding_someone_is_killed(self, monkeypatch):
        """An occupied group id is never reissued, so this one is provably
        still the child's."""
        verdict, killed = self._decide(monkeypatch, [999], True)

        assert verdict == "killed"
        assert killed == [4242]

    def test_a_group_read_completely_and_found_empty_is_left_alone(self, monkeypatch):
        verdict, killed = self._decide(monkeypatch, [], True)

        assert verdict == "empty"
        assert killed == []

    def test_a_group_that_could_not_be_read_is_not_signalled(self, monkeypatch):
        """No members seen and the scan incomplete are the same observation as
        an empty group, and one of the two readings sends a signal to whatever
        now holds a recycled id. The cost is not symmetric: the orphan left by
        refusing is a process whose identity a caller was handed and could have
        written down, and a stranger's process group is not recoverable at all.
        Could have, not did — nothing in this package writes such a record, and
        saying otherwise would credit the refusal with a recovery route that
        does not exist here."""
        verdict, killed = self._decide(monkeypatch, [], False)

        assert verdict == "unproven"
        assert killed == []

    def test_nothing_is_signalled_without_a_group_id(self, monkeypatch):
        import lionagi.providers._cli_subprocess as cs

        killed: list = []
        monkeypatch.setattr(cs, "kill_group_now", lambda pgid: killed.append(pgid))

        assert cs._kill_group_if_occupied(None) == "no-group"
        assert killed == []


class TestTheRawInputShapesTheFixWasNotWrittenAgainst:
    """The redaction was built against ``Model(**mutable_dict)`` and held there.
    Two other shapes reach the same validators and neither was covered: an
    immutable mapping through ``model_validate``, and a callback that is not
    callable at all. Both put the value back into the error the carrier exists
    to keep it out of."""

    SECRET = "raw-input-canary"

    @staticmethod
    def _channels(exc) -> dict[str, str]:
        """Every channel pydantic renders through, or the message for a plain
        exception. Asserting on one of these is how the first version of this
        protection passed while leaking."""
        errors = getattr(exc, "errors", None)
        if errors is None:
            return {"str": str(exc)}
        return {"str": str(exc), "errors": repr(exc.errors()), "json": exc.json()}

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_an_immutable_mapping_is_refused_rather_than_left_unredacted(self, model_path):
        """Substituting in place is the mechanism, not a convenience: pydantic
        keeps the object passed INTO the failing validator, so a sanitized copy
        would change nothing. A mapping that cannot be written to can only be
        leaked or refused."""
        model = _load(model_path)

        with pytest.raises(TypeError) as excinfo:
            model.model_validate(MappingProxyType({"prompt": "", "env": {"TOKEN": self.SECRET}}))

        for channel, text in self._channels(excinfo.value).items():
            assert self.SECRET not in text, f"the environment leaked through {channel}"
        # Refused for the right reason, and named so a caller can act on it.
        assert "env" in str(excinfo.value)

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_a_read_only_mapping_without_runtime_fields_still_validates(self, model_path):
        """The refusal is scoped to inputs that actually carry something to
        protect. Without this, the fix for the leak breaks every read-only
        mapping in general, which no leak test would have caught."""
        model = _load(model_path)

        request = model.model_validate(MappingProxyType({"prompt": "hi"}))

        assert request.prompt == "hi"

    @pytest.mark.parametrize("model_path", _REQUEST_MODELS)
    def test_a_callback_that_is_not_callable_is_rejected_without_being_printed(self, model_path):
        """Unwrapping the carrier is what re-exposes the value. Handing a
        non-callable back for pydantic to reject renders its ``repr`` in the
        field error, so the rejection has to happen where the unwrapping does."""
        model = _load(model_path)
        secret = self.SECRET

        class NotACallback:
            def __repr__(self):
                return f"NotACallback(token={secret!r})"

        with pytest.raises(TypeError) as excinfo:
            model(prompt="hi", on_spawn=NotACallback())

        for channel, text in self._channels(excinfo.value).items():
            assert self.SECRET not in text, f"the callback leaked through {channel}"
        # The type is named, because a type name is not a credential and a
        # caller cannot fix this without it.
        assert "NotACallback" in str(excinfo.value)


class TestRuntimeValuesArriveAfterConstructionToo:
    """The drain ran once, at construction, and construction is not the only
    way these values get in. ``EndpointConfig.update()`` puts unknown keys
    straight back into the serializable ``kwargs`` and ``iModel.from_dict()``
    assigns a hydrated config over the specialized one. Both are public, and a
    value that arrives by either is the same credential as one passed to the
    constructor."""

    SECRET = "post-construction-canary"
    PROVIDERS = ["claude_code", "codex"]

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_an_update_does_not_reach_a_serialised_model(self, provider):
        from lionagi.service.imodel import iModel

        model = iModel(provider=provider)
        model.endpoint.config.update(env={"TOKEN": self.SECRET})

        # Serialized BEFORE any payload is built, because that ordering is what
        # would leave a drain hung off create_payload unreached.
        assert self.SECRET not in json.dumps(model.to_dict())
        payload, _ = model.endpoint.create_payload({"prompt": "ok"})
        assert payload["request"].env == {"TOKEN": self.SECRET}
        assert self.SECRET not in json.dumps(model.to_dict())

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_a_hydrated_config_does_not_reach_a_serialised_model(self, provider):
        from lionagi.service.imodel import iModel

        model = iModel(provider=provider)
        payload_dict = model.to_dict()
        payload_dict["endpoint"]["config"]["kwargs"]["env"] = {"TOKEN": self.SECRET}

        rebuilt = iModel.from_dict(payload_dict)

        assert self.SECRET not in json.dumps(rebuilt.to_dict())
        payload, _ = rebuilt.endpoint.create_payload({"prompt": "ok"})
        assert payload["request"].env == {"TOKEN": self.SECRET}


class TestTheContinuationKeepsTheCallersOwnRecorder:
    """The continuation request is deep-copied, and a deep copy of a bound
    method copies its receiver. The copy would notify a duplicate supervisor
    while the real one, which holds the durable process accounting, never hears
    about the second subprocess. A stateless callback hides this entirely."""

    @pytest.mark.asyncio
    async def test_auto_finish_notifies_the_supervisor_the_caller_passed(self, monkeypatch):
        import lionagi.providers.anthropic.claude_code as cc

        class Supervisor:
            def __init__(self):
                self.seen: list = []

            def observe(self, spawned):
                self.seen.append(spawned)

        supervisor = Supervisor()
        seen_requests: list = []

        async def fake_stream(request, session, **handlers):
            # Record the request each leg was given, then hand the recorder the
            # identity it would get from a real spawn.
            seen_requests.append(request)
            on_spawn = getattr(request, "on_spawn", None)
            if on_spawn is not None:
                on_spawn(f"leg-{len(seen_requests)}")
            yield cc.StreamChunk(type="system", metadata={})

        monkeypatch.setattr(cc, "stream_claude_code_cli", fake_stream)

        endpoint = cc.ClaudeCodeCLIEndpoint()
        request = cc.ClaudeCodeRequest(prompt="ok", auto_finish=True, on_spawn=supervisor.observe)
        await endpoint._call({"request": request}, {})

        # Both legs ran, and both reported to the object the caller handed in.
        assert len(seen_requests) == 2, "auto_finish did not produce a continuation leg"
        assert supervisor.seen == ["leg-1", "leg-2"]
        assert seen_requests[1] is not seen_requests[0]
        assert seen_requests[1].on_spawn.__self__ is supervisor


class TestIdentityIsEstablishedTwoWaysNotOne:
    """A recorded group id is provably still this child's under either of two
    facts, and they cover different moments. While the child is unreaped its
    pid cannot have been reissued. Once it is reaped, only a live member pins
    the id. Checking only the second refuses where identity was never in
    question; checking only the first signals a number that may name a
    stranger."""

    def test_an_unreaped_child_is_ended_without_consulting_the_scan(self, monkeypatch):
        """This is the half that was missing. On the cancellation backstop the
        direct child has not been waited, so its pid cannot have been reissued
        and the group is provably its own. Refusing here because the process
        table could not be read left a SIGTERM-ignoring descendant alive on
        every platform that cannot enumerate."""
        import lionagi.providers._cli_subprocess as cs

        signalled: list = []
        scanned: list = []
        monkeypatch.setattr(cs, "kill_group_now", lambda pgid: signalled.append(pgid) or True)
        monkeypatch.setattr(
            cs, "group_member_pids", lambda pgid: (scanned.append(pgid), ([], False))[1]
        )

        verdict = cs._end_group_with_evidence(SimpleNamespace(pid=4242, returncode=None))

        assert verdict == "killed-unreaped"
        assert signalled == [4242]
        # The scan is not merely ignored, it is not reached: an unreadable
        # process table must not be able to change this answer.
        assert scanned == []

    def test_a_reaped_child_still_defers_to_the_scan(self, monkeypatch):
        """The other side of the same rule, so the fix cannot be satisfied by
        always killing. Once reaped, the pid is just a number."""
        import lionagi.providers._cli_subprocess as cs

        signalled: list = []
        monkeypatch.setattr(cs, "kill_group_now", lambda pgid: signalled.append(pgid) or True)
        monkeypatch.setattr(cs, "group_member_pids", lambda pgid: ([], False))

        verdict = cs._end_group_with_evidence(SimpleNamespace(pid=4242, returncode=0))

        assert verdict == "unproven"
        assert signalled == []

    def test_a_completed_but_reaped_spawn_is_not_signalled(self, monkeypatch):
        """A spawn task can complete AND its child be reaped before the
        done-callback runs. The pid is then just a number, and the group scan
        is the only thing that can say whether it still names this child."""
        import lionagi.providers._cli_subprocess as cs

        signalled: list = []
        monkeypatch.setattr(cs, "kill_group_now", lambda pgid: signalled.append(pgid) or True)
        monkeypatch.setattr(cs, "group_member_pids", lambda pgid: ([], True))

        class CompletedSpawn:
            def cancelled(self):
                return False

            def exception(self):
                return None

            def result(self):
                return SimpleNamespace(pid=4242, returncode=0)

        cs._kill_abandoned_spawn(CompletedSpawn())

        assert signalled == []

    def test_a_completed_and_still_running_spawn_is_signalled(self, monkeypatch):
        """The other half, so the fix above cannot be satisfied by never
        signalling at all. Unreaped means the id is still this child's."""
        import lionagi.providers._cli_subprocess as cs

        signalled: list = []
        monkeypatch.setattr(cs, "kill_group_now", lambda pgid: signalled.append(pgid) or True)

        class RunningSpawn:
            def cancelled(self):
                return False

            def exception(self):
                return None

            def result(self):
                return SimpleNamespace(pid=4242, returncode=None)

        cs._kill_abandoned_spawn(RunningSpawn())

        assert signalled == [4242]
