# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A caller that must record the process identity of a CLI leg it did not spawn
itself reads that identity through ``ndjson_from_cli``'s ``on_spawn`` callback,
and supplies the leg's environment through ``env``.

These run real subprocesses. The identity being tested is the kernel's, and a
mocked ``create_subprocess_exec`` would let a wrong pgid read pass: the whole
point of reading the group at spawn is that the answer stops being available
once the child is reaped.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from lionagi.providers._cli_subprocess import ndjson_from_cli, spawned_pgid

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

# Prints the two variables the environment tests care about, as NDJSON.
_ECHO_ENV = (
    "import json, os, sys; "
    "sys.stdout.write(json.dumps({'type': 'env', "
    "'inherited': os.environ.get('LIONAGI_TEST_INHERITED'), "
    "'supplied': os.environ.get('LIONAGI_TEST_SUPPLIED')}) + '\\n')"
)


def _cmd(script: str) -> list[str]:
    return [sys.executable, "-c", script]


async def _drain(stream) -> list[dict]:
    return [obj async for obj in stream]


class TestSpawnObservation:
    @pytest.mark.asyncio
    async def test_reports_the_live_child_pid_and_its_real_process_group(self):
        seen: list[tuple[int, int]] = []
        first_object_before_callback = []

        async def run():
            async for obj in ndjson_from_cli(
                _cmd(_SLEEPER), on_spawn=lambda pid, pgid: seen.append((pid, pgid))
            ):
                first_object_before_callback.append(obj)
                # The child is still alive here, so the kernel can still be
                # asked what the callback claimed.
                pid, pgid = seen[0]
                assert os.getpgid(pid) == pgid
                break

        task = asyncio.create_task(run())
        await asyncio.wait_for(task, timeout=30)

        assert len(seen) == 1
        pid, pgid = seen[0]
        # start_new_session=True: the child leads its own group, so the group
        # is the child's own pid and never this test process's.
        assert pgid == pid
        assert pgid != os.getpgid(0)
        assert first_object_before_callback == [{"type": "hello"}]

    @pytest.mark.asyncio
    async def test_callback_fires_before_any_object_is_yielded(self):
        """The ordering IS the contract: a caller arming a per-leg timeout or
        recording a quiescence-domain member must have the identity before the
        leg can produce anything the caller might act on."""
        order: list[str] = []

        async for _ in ndjson_from_cli(
            _cmd(_QUICK), on_spawn=lambda pid, pgid: order.append("spawn")
        ):
            order.append("object")

        assert order == ["spawn", "object"]

    @pytest.mark.asyncio
    async def test_identity_is_recorded_even_for_a_child_that_exits_at_once(self):
        """A group read deferred to teardown cannot answer for a reaped child.
        The recorded group is a usable identity here only because it was read
        while the child still existed."""
        seen: list[tuple[int, int]] = []

        objs = await _drain(
            ndjson_from_cli(_cmd(_QUICK), on_spawn=lambda pid, pgid: seen.append((pid, pgid)))
        )

        assert objs == [{"type": "hello"}]
        assert len(seen) == 1
        pid, pgid = seen[0]
        assert pgid == pid

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

        def boom(pid: int, pgid: int) -> None:
            seen.append(pid)
            raise RecorderFailed("cannot record")

        with pytest.raises(RecorderFailed):
            await _drain(ndjson_from_cli(_cmd(_SLEEPER), on_spawn=boom))

        assert len(seen) == 1
        pid = seen[0]
        # The child was a 30s sleeper: still being here means it was ended
        # rather than having finished on its own.
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - only reached if teardown did not run
            pytest.fail(f"child {pid} still alive after the recorder failed")


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

        req = ClaudeCodeRequest(prompt="hi", env={"A": "1"}, on_spawn=lambda pid, pgid: None)

        assert req.env == {"A": "1"}
        assert callable(req.on_spawn)
        dumped = req.model_dump()
        assert "env" not in dumped and "on_spawn" not in dumped
        args = req.as_cmd_args()
        assert not [a for a in args if "env" in a.lower() or "spawn" in a.lower()]

    def test_codex_request_keeps_them_off_the_wire(self):
        from lionagi.providers.openai.codex import CodexCodeRequest

        req = CodexCodeRequest(prompt="hi", env={"A": "1"}, on_spawn=lambda pid, pgid: None)

        assert req.env == {"A": "1"}
        assert callable(req.on_spawn)
        dumped = req.model_dump()
        assert "env" not in dumped and "on_spawn" not in dumped
        args = req.as_cmd_args()
        assert not [a for a in args if "env" in a.lower() or "spawn" in a.lower()]

    def test_both_default_to_absent(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
        from lionagi.providers.openai.codex import CodexCodeRequest

        for req in (ClaudeCodeRequest(prompt="hi"), CodexCodeRequest(prompt="hi")):
            assert req.env is None
            assert req.on_spawn is None

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

        recorder = lambda pid, pgid: None  # noqa: E731 - identity is what is asserted
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

        recorder = lambda pid, pgid: None  # noqa: E731 - identity is what is asserted
        req = cx.CodexCodeRequest(prompt="hi", env={"A": "1"}, on_spawn=recorder)
        await _drain(cx.stream_codex_cli_events(req))

        assert captured["env"] == {"A": "1"}
        assert captured["on_spawn"] is recorder
