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
import os
import sys

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

        async def run():
            async for _ in ndjson_from_cli(_cmd(_SLEEPER), on_spawn=seen.append):
                break

        task = asyncio.create_task(run())
        await asyncio.wait_for(task, timeout=30)

        assert len(seen) == 1
        spawned = seen[0]
        assert spawned.create_time is not None
        # The value is this child's own, not a constant and not the parent's.
        assert spawned.create_time != spawned_create_time(os.getpid())
        assert spawned_create_time(spawned.pid) == spawned.create_time

    @pytest.mark.asyncio
    async def test_identity_is_recorded_even_for_a_child_that_exits_at_once(self):
        """A read deferred to teardown cannot answer for a reaped child. The
        recorded group is a usable identity here only because it was read while
        the child still existed."""
        seen: list[SpawnedProcess] = []

        objs = await _drain(ndjson_from_cli(_cmd(_QUICK), on_spawn=seen.append))

        assert objs == [{"type": "hello"}]
        assert len(seen) == 1
        assert seen[0].pgid == seen[0].pid

    @pytest.mark.asyncio
    async def test_an_async_recorder_is_awaited_before_the_stream_is_read(self):
        """A durable recorder is written in the runner's own async style, and
        ``Callable[..., None]`` does not reject an ``async def`` at runtime: an
        un-awaited one would return a coroutine that is quietly dropped, so the
        leg runs entirely unrecorded and nothing raises. The recording finishes
        before the first object arrives, so a consumer that acts on the stream
        can never be ahead of the record."""
        recorded: list[SpawnedProcess] = []

        async def recorder(spawned: SpawnedProcess) -> None:
            await asyncio.sleep(0)
            recorded.append(spawned)

        objs = await _drain(ndjson_from_cli(_cmd(_QUICK), on_spawn=recorder))

        assert objs == [{"type": "hello"}]
        assert len(recorded) == 1
        assert recorded[0].pid > 0

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
        the caller asked for at the call site."""
        endpoint_cls = _load(endpoint_path)
        request_cls = _load(request_path)

        payload, _ = endpoint_cls().create_payload(
            request_cls(prompt="hi", env={"A": "1"}), env={"B": "2"}
        )

        assert payload["request"].env == {"B": "2"}

    def test_a_request_passed_as_a_dict_is_unaffected(self):
        """A dict never went through a dump, so its values were never at risk.
        This is the canonical iModel route and it must keep behaving as it did.
        """
        endpoint_cls = _load("lionagi.providers.anthropic.claude_code:ClaudeCodeCLIEndpoint")

        payload, _ = endpoint_cls().create_payload({"prompt": "hi", "env": {"A": "1"}})

        assert payload["request"].env == {"A": "1"}


def _load(path: str):
    module_name, _, attr = path.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), attr)
