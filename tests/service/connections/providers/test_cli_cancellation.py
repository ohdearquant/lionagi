"""Regression tests for CLI subprocess cancellation (Ctrl+C / SIGINT).

Covers start_new_session isolation, CancelledError propagation without auto_finish, and aclosing() cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.providers._executable_fixtures import cli_executable

_SYNTHETIC_PROCESS_IDS = frozenset({8101, 8102, 8103, 8104})
_MOCK_PROCESSES: dict[int, MagicMock] = {}


@pytest.fixture(autouse=True)
def _block_real_process_group_signals(monkeypatch):
    """Reject real signals; only completed, enumerated fake children report absence."""
    import lionagi.ln._proc as proc_mod

    _MOCK_PROCESSES.clear()

    def fail_if_unmocked(pgid, sig):
        proc = _MOCK_PROCESSES.get(pgid)
        if (
            sig == 0
            and pgid in _SYNTHETIC_PROCESS_IDS
            and proc is not None
            and type(proc.returncode) is int
        ):
            raise ProcessLookupError(pgid)
        raise AssertionError(f"unmocked os.killpg({pgid}, {sig}) in subprocess unit test")

    if hasattr(proc_mod.os, "killpg"):
        monkeypatch.setattr(proc_mod.os, "killpg", fail_if_unmocked)
    try:
        yield
    finally:
        _MOCK_PROCESSES.clear()


def _exited_group_probe(proc, signal_calls):
    """Model an exited fake child's absence without accepting a group signal."""

    def probe(pgid, sig):
        if pgid == proc.pid and sig == 0 and type(proc.returncode) is int:
            raise ProcessLookupError(pgid)
        signal_calls.append((pgid, sig))
        raise AssertionError(f"unexpected group signal or premature absence probe: {pgid}, {sig}")

    return probe


def _spawn_call_for(mock_exec, executable: str):
    """Return the provider spawn, excluding credential-helper subprocesses."""
    matches = [
        call
        for call in mock_exec.call_args_list
        if call.args and str(call.args[0]).rsplit("/", 1)[-1] == executable
    ]
    assert len(matches) == 1
    return matches[0]


class TestSubprocessSessionIsolation:
    """Verify CLI subprocesses are isolated from the parent's process group."""

    @pytest.mark.asyncio
    async def test_claude_cli_uses_start_new_session(self, tmp_path):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        request = ClaudeCodeRequest(prompt="test")

        from lionagi.providers.anthropic.claude_code import _ndjson_from_cli

        with (
            patch(
                "lionagi.providers.anthropic.claude_code.CLAUDE_CLI",
                cli_executable(tmp_path, "claude"),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_proc = _make_mock_proc(pid=8101)
            mock_exec.return_value = mock_proc

            chunks = []
            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for obj in stream:
                    chunks.append(obj)

            # Verify start_new_session=True was passed
            _, kwargs = _spawn_call_for(mock_exec, "claude")
            assert kwargs.get("start_new_session") is True, (
                "CLI subprocess must use start_new_session=True to isolate from SIGINT"
            )

    @pytest.mark.asyncio
    async def test_codex_cli_uses_start_new_session(self, tmp_path):
        from lionagi.providers.openai.codex import CodexCodeRequest

        request = CodexCodeRequest(prompt="test")

        from lionagi.providers.openai.codex import _ndjson_from_cli

        with (
            patch("lionagi.providers.openai.codex.CODEX_CLI", cli_executable(tmp_path, "codex")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_proc = _make_mock_proc(pid=8102)
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

            _, kwargs = _spawn_call_for(mock_exec, "codex")
            assert kwargs.get("start_new_session") is True

    @pytest.mark.asyncio
    async def test_gemini_cli_uses_start_new_session(self, tmp_path):
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

        request = GeminiCodeRequest(prompt="test")

        from lionagi.providers.google.gemini_code import _ndjson_from_cli

        with (
            patch("lionagi.providers.google.gemini_code.AGY_CLI", cli_executable(tmp_path, "agy")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_proc = _make_mock_proc(pid=8103)
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

            _, kwargs = _spawn_call_for(mock_exec, "agy")
            assert kwargs.get("start_new_session") is True


class TestCancellationSkipsAutoFinish:
    @pytest.mark.asyncio
    async def test_cancelled_error_skips_auto_finish(self):
        from lionagi.providers.anthropic.claude_code import (
            ClaudeCodeCLIEndpoint,
        )

        stream_call_count = 0

        async def cancelling_stream(*args, **kwargs):
            nonlocal stream_call_count
            stream_call_count += 1
            yield MagicMock(text="partial")
            raise asyncio.CancelledError()

        with patch(
            "lionagi.providers.anthropic.claude_code.stream_claude_code_cli",
            side_effect=cancelling_stream,
        ):
            endpoint = ClaudeCodeCLIEndpoint()

            mock_request = MagicMock()
            mock_request.auto_finish = True  # would trigger second spawn normally
            mock_request.cli_include_summary = False

            with pytest.raises(asyncio.CancelledError):
                await endpoint._call({"request": mock_request}, {})

            # stream_claude_code_cli must only be called ONCE —
            # the second (auto_finish) call must never happen
            assert stream_call_count == 1, (
                f"auto_finish spawned {stream_call_count - 1} extra session(s) after cancellation"
            )

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_skips_auto_finish(self):
        from lionagi.providers.anthropic.claude_code import (
            ClaudeCodeCLIEndpoint,
        )

        stream_call_count = 0

        async def interrupting_stream(*args, **kwargs):
            nonlocal stream_call_count
            stream_call_count += 1
            yield MagicMock(text="partial")
            raise KeyboardInterrupt()

        with patch(
            "lionagi.providers.anthropic.claude_code.stream_claude_code_cli",
            side_effect=interrupting_stream,
        ):
            endpoint = ClaudeCodeCLIEndpoint()

            mock_request = MagicMock()
            mock_request.auto_finish = True
            mock_request.cli_include_summary = False

            with pytest.raises(KeyboardInterrupt):
                await endpoint._call({"request": mock_request}, {})

            assert stream_call_count == 1

    @pytest.mark.asyncio
    async def test_task_cancel_skips_auto_finish(self):
        """Task.cancel() must not trigger auto_finish even when subprocess exits gracefully."""
        from lionagi.providers.anthropic.claude_code import (
            ClaudeCodeCLIEndpoint,
        )

        stream_call_count = 0

        async def stream_then_cancel(*args, **kwargs):
            nonlocal stream_call_count
            stream_call_count += 1
            yield MagicMock(text="partial")
            # Simulate external task cancellation (e.g., from executor timeout)
            raise asyncio.CancelledError()

        with patch(
            "lionagi.providers.anthropic.claude_code.stream_claude_code_cli",
            side_effect=stream_then_cancel,
        ):
            endpoint = ClaudeCodeCLIEndpoint()

            mock_request = MagicMock()
            mock_request.auto_finish = True
            mock_request.cli_include_summary = False

            with pytest.raises(asyncio.CancelledError):
                await endpoint._call({"request": mock_request}, {})

            assert stream_call_count == 1, "Task.cancel() must not trigger auto_finish"

    @pytest.mark.asyncio
    async def test_normal_completion_still_triggers_auto_finish(self):
        from lionagi.providers.anthropic.claude_code import (
            ClaudeCodeCLIEndpoint,
            ClaudeSession,
        )

        stream_call_count = 0
        mock_session = MagicMock(spec=ClaudeSession)
        mock_session.session_id = "test"
        mock_session.chunks = []
        mock_session.result = "done"

        async def normal_stream(*args, **kwargs):
            nonlocal stream_call_count
            stream_call_count += 1
            if stream_call_count == 1:
                # First call: partial result (not a ClaudeSession)
                yield MagicMock(text="partial response")
            else:
                # Second call (auto_finish): return session
                yield mock_session

        with patch(
            "lionagi.providers.anthropic.claude_code.stream_claude_code_cli",
            side_effect=normal_stream,
        ):
            endpoint = ClaudeCodeCLIEndpoint()

            mock_request = MagicMock()
            mock_request.auto_finish = True
            mock_request.cli_include_summary = False
            mock_request.model_copy = MagicMock(
                return_value=MagicMock(
                    prompt="finish",
                    max_turns=1,
                    continue_conversation=True,
                )
            )

            result = await endpoint._call({"request": mock_request}, {})

            # auto_finish SHOULD trigger normally
            assert stream_call_count == 2, "auto_finish should fire on normal completion"


class TestNdjsonCleanupPropagation:
    """Verify _ndjson_from_cli finally block doesn't swallow CancelledError."""

    @pytest.mark.asyncio
    async def test_cancelled_error_not_swallowed_by_finally(self, tmp_path):
        # Fix: except asyncio.TimeoutError (not CancelledError)
        from lionagi.providers.anthropic.claude_code import (
            ClaudeCodeRequest,
            _ndjson_from_cli,
        )

        request = ClaudeCodeRequest(prompt="test")

        with (
            patch(
                "lionagi.providers.anthropic.claude_code.CLAUDE_CLI",
                cli_executable(tmp_path, "claude"),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 8104
            _MOCK_PROCESSES[8104] = mock_proc
            child_exited = asyncio.Event()

            read_call = 0

            async def slow_read(n):
                nonlocal read_call
                read_call += 1
                if read_call == 1:
                    return b'{"type": "system", "session_id": "s1"}\n'
                # Simulate cancellation during second read
                raise asyncio.CancelledError()

            # Not waited, so still None: this child is cancelled mid-stream,
            # and an unset MagicMock here would report it as already reaped.
            mock_proc.returncode = None
            mock_proc.stdout = MagicMock()
            mock_proc.stdout.read = slow_read
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"")

            async def wait_for_exit():
                await child_exited.wait()
                return mock_proc.returncode

            def stop_child(returncode):
                if mock_proc.returncode is None:
                    mock_proc.returncode = returncode
                child_exited.set()

            mock_proc.wait = AsyncMock(side_effect=wait_for_exit)
            mock_proc.terminate = MagicMock(side_effect=lambda: stop_child(-15))
            mock_proc.kill = MagicMock(side_effect=lambda: stop_child(-9))
            mock_exec.return_value = mock_proc

            with pytest.raises(asyncio.CancelledError):
                async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                    async for _ in stream:
                        pass

            # Verify cleanup was attempted
            mock_proc.terminate.assert_called()


class TestToolAllowlistArgs:
    """Verify tool names are passed without embedded quotes to subprocess."""

    def test_allowed_tools_no_embedded_quotes(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        request = ClaudeCodeRequest(
            prompt="test",
            allowed_tools=["Bash", "Read", "Write"],
        )
        args = request.as_cmd_args()

        # Find the tools after --allowedTools
        idx = args.index("--allowedTools")
        tool_args = []
        for a in args[idx + 1 :]:
            if a.startswith("--"):
                break
            tool_args.append(a)

        for tool in tool_args:
            assert '"' not in tool, (
                f"Tool name {tool!r} has embedded quotes — "
                "subprocess_exec passes args verbatim, quotes break matching"
            )
            assert tool in {"Bash", "Read", "Write"}

    def test_disallowed_tools_no_embedded_quotes(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        request = ClaudeCodeRequest(
            prompt="test",
            disallowed_tools=["Edit", "Write"],
        )
        args = request.as_cmd_args()

        idx = args.index("--disallowedTools")
        tool_args = []
        for a in args[idx + 1 :]:
            if a.startswith("--"):
                break
            tool_args.append(a)

        for tool in tool_args:
            assert '"' not in tool

    def test_mcp_config_no_embedded_quotes(self):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        request = ClaudeCodeRequest(
            prompt="test",
            mcp_config=".mcp/config.json",
        )
        args = request.as_cmd_args()

        idx = args.index("--mcp-config")
        config_val = args[idx + 1]
        assert '"' not in config_val, f"mcp_config value {config_val!r} has embedded quotes"


class TestSessionIsolation:
    """Verify each stream_claude_code_cli call gets its own session."""

    def test_default_session_is_none_not_shared_instance(self):
        import inspect

        from lionagi.providers.anthropic.claude_code import (
            stream_claude_code_cli,
        )

        sig = inspect.signature(stream_claude_code_cli)
        session_param = sig.parameters["session"]
        assert session_param.default is None, (
            f"session default is {session_param.default!r}, not None — "
            "mutable default causes cross-request data leakage"
        )


class TestEmptyResponsesGuard:
    """Verify _call handles empty response list without IndexError."""

    @pytest.mark.asyncio
    async def test_auto_finish_with_empty_responses(self):
        from lionagi.providers.anthropic.claude_code import (
            ClaudeCodeCLIEndpoint,
        )

        async def empty_stream(*args, **kwargs):
            return
            yield  # make it an async generator

        with patch(
            "lionagi.providers.anthropic.claude_code.stream_claude_code_cli",
            side_effect=empty_stream,
        ):
            endpoint = ClaudeCodeCLIEndpoint()

            mock_request = MagicMock()
            mock_request.auto_finish = True
            mock_request.cli_include_summary = False

            # Must not raise IndexError on responses[-1]
            result = await endpoint._call({"request": mock_request}, {})
            assert isinstance(result, dict)


def _make_mock_proc(pid=12345):
    """Build a minimal mock subprocess suitable for ndjson_from_cli tests."""
    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = AsyncMock(return_value=b"")
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"")
    # A real process reports None until it has been waited and an int after.
    # Left unset this is a MagicMock, which is not None, so anything asking
    # whether the child has been reaped is told yes about one still running --
    # and teardown decisions turn on exactly that question.
    mock_proc.returncode = None

    async def _wait():
        mock_proc.returncode = 0
        return 0

    mock_proc.wait = AsyncMock(side_effect=_wait)
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    if pid in _SYNTHETIC_PROCESS_IDS:
        _MOCK_PROCESSES[pid] = mock_proc
    return mock_proc


class TestProcessGroupCleanup:
    """Gemini and Pi share the same identity-gated process-group cleanup.

    Claude and Codex capture the PGID immediately after spawn and call
    os.killpg(pgid, SIGTERM/SIGKILL) in cleanup so that descendant processes
    started by a live CLI are also terminated. Once the direct child is reaped,
    a bare numeric pid cannot authorize that group-wide signal.
    """

    @pytest.mark.asyncio
    async def test_gemini_reaped_cleanup_does_not_signal_a_numeric_group(self, tmp_path):
        from lionagi.providers.google.gemini_code import (
            GeminiCodeRequest,
            _ndjson_from_cli,
        )

        request = GeminiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9001)
        killpg_calls: list[tuple] = []

        with (
            patch("lionagi.providers.google.gemini_code.AGY_CLI", cli_executable(tmp_path, "agy")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch(
                "lionagi.ln._proc.os.killpg",
                side_effect=_exited_group_probe(mock_proc, killpg_calls),
            ),
        ):
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        assert killpg_calls == [], (
            "Gemini reaped the direct child before teardown, so pid 9001 is now "
            f"recyclable and cannot authorize a group signal: {killpg_calls}"
        )

    @pytest.mark.asyncio
    async def test_pi_reaped_cleanup_does_not_signal_a_numeric_group(self, tmp_path):
        from lionagi.providers.pi.cli import PiCodeRequest, _ndjson_from_cli

        request = PiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9002)
        killpg_calls: list[tuple] = []

        with (
            patch("lionagi.providers.pi.cli.PI_CLI", cli_executable(tmp_path, "pi")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch(
                "lionagi.ln._proc.os.killpg",
                side_effect=_exited_group_probe(mock_proc, killpg_calls),
            ),
        ):
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        assert killpg_calls == [], (
            "Pi reaped the direct child before teardown, so pid 9002 is now "
            f"recyclable and cannot authorize a group signal: {killpg_calls}"
        )

    @pytest.mark.asyncio
    async def test_gemini_mock_pid_guard_skips_killpg(self, tmp_path):
        """pid=1 must be filtered by the safety guard, not passed to os.killpg."""
        from lionagi.providers.google.gemini_code import (
            GeminiCodeRequest,
            _ndjson_from_cli,
        )

        request = GeminiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc()
        # Simulate a MagicMock pid (like in test environments where proc.pid is
        # not a real int but isinstance check still passes if set to 1).
        mock_proc.pid = 1  # pid=1 should be filtered out by the safety guard

        killpg_calls: list = []

        with (
            patch("lionagi.providers.google.gemini_code.AGY_CLI", cli_executable(tmp_path, "agy")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch(
                "lionagi.ln._proc.os.killpg",
                side_effect=lambda pgid, sig: killpg_calls.append((pgid, sig)),
            ),
        ):
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        # pid=1 must be blocked by the isinstance + pid > 1 guard.
        assert killpg_calls == [], (
            f"os.killpg was called with pid=1: {killpg_calls}. "
            "The safety guard must prevent signalling init/CI runner."
        )

    @pytest.mark.asyncio
    async def test_gemini_cleanup_no_killpg_platform(self, tmp_path, monkeypatch):
        """When os.killpg is absent (Windows), cleanup must fall through to proc.terminate()."""
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.google import gemini_code as models

        request = models.GeminiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9101)

        # Simulate Windows: os.killpg does not exist.
        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch("lionagi.providers.google.gemini_code.AGY_CLI", cli_executable(tmp_path, "agy")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc

            # Must complete cleanly — no AttributeError leaking from cleanup.
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        # Reaching here is the assertion: cleanup ran to completion with no
        # AttributeError from the absent killpg. Asserting terminate() was
        # called instead pins a call that does nothing on a child which has
        # already exited, and would pass whether or not the group was dealt with.

    @pytest.mark.asyncio
    async def test_pi_cleanup_no_killpg_platform(self, tmp_path, monkeypatch):
        """When os.killpg is absent (Windows), Pi cleanup must fall through to proc.terminate()."""
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.pi import cli as models

        request = models.PiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9102)

        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch("lionagi.providers.pi.cli.PI_CLI", cli_executable(tmp_path, "pi")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        # Reaching here is the assertion: cleanup ran to completion with no
        # AttributeError from the absent killpg. Asserting terminate() was
        # called instead pins a call that does nothing on a child which has
        # already exited, and would pass whether or not the group was dealt with.


class TestKillpgUnavailablePlatform:
    """os.killpg is POSIX-only. On Windows a real pid still set _pgid, so the
    finally-block killpg call raised AttributeError (only ProcessLookupError /
    PermissionError suppressed), failing even a successful run. Cleanup must
    fall through to proc.terminate() instead."""

    @pytest.mark.asyncio
    async def test_claude_cleanup_no_killpg_platform(self, tmp_path, monkeypatch):
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.anthropic import claude_code as models

        request = models.ClaudeCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9201)
        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch(
                "lionagi.providers.anthropic.claude_code.CLAUDE_CLI",
                cli_executable(tmp_path, "claude"),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        # Reaching here is the assertion: cleanup ran to completion with no
        # AttributeError from the absent killpg. Asserting terminate() was
        # called instead pins a call that does nothing on a child which has
        # already exited, and would pass whether or not the group was dealt with.

    @pytest.mark.asyncio
    async def test_codex_cleanup_no_killpg_platform(self, tmp_path, monkeypatch):
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.openai import codex as models

        request = models.CodexCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9202)
        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch("lionagi.providers.openai.codex.CODEX_CLI", cli_executable(tmp_path, "codex")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        # Reaching here is the assertion: cleanup ran to completion with no
        # AttributeError from the absent killpg. Asserting terminate() was
        # called instead pins a call that does nothing on a child which has
        # already exited, and would pass whether or not the group was dealt with.

    @pytest.mark.asyncio
    async def test_unrecorded_launch_warns_when_group_cleanup_is_unconfirmed(
        self, tmp_path, monkeypatch, caplog
    ):
        """With no worker recorder bound, a successful run whose group cannot be read
        still completes, and the unconfirmed cleanup is reported as a warning."""
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.anthropic import claude_code as models

        request = models.ClaudeCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9203)
        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch(
                "lionagi.providers.anthropic.claude_code.CLAUDE_CLI",
                cli_executable(tmp_path, "claude"),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            caplog.at_level(logging.WARNING, logger="lionagi.providers._cli_subprocess"),
        ):
            mock_exec.return_value = mock_proc
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "lionagi.providers._cli_subprocess"
            and "CLI child group cleanup remains unconfirmed" in record.getMessage()
        ], "an unconfirmed cleanup on an unrecorded launch ended with no warning"


class TestStderrDeadlockPrevention:
    """Gemini and Pi drain stderr concurrently; a large payload must not deadlock."""

    @pytest.mark.asyncio
    async def test_gemini_large_stderr_does_not_deadlock(self, tmp_path):
        import asyncio

        from lionagi.providers.google.gemini_code import (
            GeminiCodeRequest,
            _ndjson_from_cli,
        )

        request = GeminiCodeRequest(prompt="test")
        large_stderr = b"E: " + b"x" * 65536  # 64 KB — fills a typical pipe buffer

        mock_proc = MagicMock()
        mock_proc.pid = 9003
        mock_proc.returncode = None
        mock_proc.stdout = MagicMock()
        # stdout returns empty immediately (no JSON output)
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        stderr_reads = [large_stderr, b""]
        stderr_idx = 0

        async def fake_stderr_read(n):
            nonlocal stderr_idx
            val = stderr_reads[min(stderr_idx, len(stderr_reads) - 1)]
            stderr_idx += 1
            return val

        mock_proc.stderr.read = fake_stderr_read

        async def wait_for_exit():
            mock_proc.returncode = 1
            return 1

        mock_proc.wait = AsyncMock(side_effect=wait_for_exit)  # non-zero exit to test error path
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        with (
            patch("lionagi.providers.google.gemini_code.AGY_CLI", cli_executable(tmp_path, "agy")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("lionagi.ln._proc.os.killpg"),
        ):
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError) as exc_info:
                async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                    async for _ in stream:
                        pass

        # The error message should include the captured stderr content.
        assert "x" * 10 in str(exc_info.value), (
            "Stderr content was not captured in the error — concurrent drain may be broken"
        )

    @pytest.mark.asyncio
    async def test_pi_large_stderr_does_not_deadlock(self, tmp_path):
        from lionagi.providers.pi.cli import PiCodeRequest, _ndjson_from_cli

        request = PiCodeRequest(prompt="test")
        large_stderr = b"E: " + b"y" * 65536

        mock_proc = MagicMock()
        mock_proc.pid = 9004
        mock_proc.returncode = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        stderr_reads = [large_stderr, b""]
        stderr_idx = 0

        async def fake_stderr_read(n):
            nonlocal stderr_idx
            val = stderr_reads[min(stderr_idx, len(stderr_reads) - 1)]
            stderr_idx += 1
            return val

        mock_proc.stderr.read = fake_stderr_read

        async def wait_for_exit():
            mock_proc.returncode = 1
            return 1

        mock_proc.wait = AsyncMock(side_effect=wait_for_exit)
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        with (
            patch("lionagi.providers.pi.cli.PI_CLI", cli_executable(tmp_path, "pi")),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("lionagi.ln._proc.os.killpg"),
        ):
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError) as exc_info:
                async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                    async for _ in stream:
                        pass

        assert "y" * 10 in str(exc_info.value)
