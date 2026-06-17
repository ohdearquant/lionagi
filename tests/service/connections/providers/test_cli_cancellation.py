"""Regression tests for CLI subprocess cancellation (Ctrl+C / SIGINT).

Covers start_new_session isolation, CancelledError propagation without auto_finish, and aclosing() cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. start_new_session=True is always passed to subprocess creation
# ---------------------------------------------------------------------------


class TestSubprocessSessionIsolation:
    """Verify CLI subprocesses are isolated from the parent's process group."""

    @pytest.mark.asyncio
    async def test_claude_cli_uses_start_new_session(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        request = ClaudeCodeRequest(prompt="test")

        from lionagi.providers.anthropic.claude_code.models import _ndjson_from_cli

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stdout.read = AsyncMock(return_value=b"")
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"")
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.terminate = MagicMock()
            mock_proc.kill = MagicMock()
            mock_exec.return_value = mock_proc

            chunks = []
            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for obj in stream:
                    chunks.append(obj)

            # Verify start_new_session=True was passed
            mock_exec.assert_called_once()
            _, kwargs = mock_exec.call_args
            assert kwargs.get("start_new_session") is True, (
                "CLI subprocess must use start_new_session=True to isolate from SIGINT"
            )

    @pytest.mark.asyncio
    async def test_codex_cli_uses_start_new_session(self):
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        request = CodexCodeRequest(prompt="test")

        from lionagi.providers.openai.codex.models import _ndjson_from_cli

        with (
            patch("lionagi.providers.openai.codex.models.CODEX_CLI", "codex"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stdout.read = AsyncMock(return_value=b"")
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"")
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.terminate = MagicMock()
            mock_proc.kill = MagicMock()
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

            mock_exec.assert_called_once()
            _, kwargs = mock_exec.call_args
            assert kwargs.get("start_new_session") is True

    @pytest.mark.asyncio
    async def test_gemini_cli_uses_start_new_session(self):
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        request = GeminiCodeRequest(prompt="test")

        from lionagi.providers.google.gemini_code.models import _ndjson_from_cli

        with (
            patch("lionagi.providers.google.gemini_code.models.GEMINI_CLI", "gemini"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stdout.read = AsyncMock(return_value=b"")
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"")
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.terminate = MagicMock()
            mock_proc.kill = MagicMock()
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

            mock_exec.assert_called_once()
            _, kwargs = mock_exec.call_args
            assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# 2. CancelledError propagates through _call() without triggering auto_finish
# ---------------------------------------------------------------------------


class TestCancellationSkipsAutoFinish:
    """Verify that task cancellation never triggers auto_finish."""

    @pytest.mark.asyncio
    async def test_cancelled_error_skips_auto_finish(self):
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        stream_call_count = 0

        async def cancelling_stream(*args, **kwargs):
            nonlocal stream_call_count
            stream_call_count += 1
            yield MagicMock(text="partial")
            raise asyncio.CancelledError()

        with patch(
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli",
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
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        stream_call_count = 0

        async def interrupting_stream(*args, **kwargs):
            nonlocal stream_call_count
            stream_call_count += 1
            yield MagicMock(text="partial")
            raise KeyboardInterrupt()

        with patch(
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli",
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
        from lionagi.providers.anthropic.claude_code.endpoint import (
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
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli",
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
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )
        from lionagi.providers.anthropic.claude_code.models import ClaudeSession

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
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli",
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


# ---------------------------------------------------------------------------
# 3. _ndjson_from_cli cleanup: CancelledError must not be swallowed
# ---------------------------------------------------------------------------


class TestNdjsonCleanupPropagation:
    """Verify _ndjson_from_cli finally block doesn't swallow CancelledError."""

    @pytest.mark.asyncio
    async def test_cancelled_error_not_swallowed_by_finally(self):
        # Fix: except asyncio.TimeoutError (not CancelledError)
        from lionagi.providers.anthropic.claude_code.models import (
            ClaudeCodeRequest,
            _ndjson_from_cli,
        )

        request = ClaudeCodeRequest(prompt="test")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()

            read_call = 0

            async def slow_read(n):
                nonlocal read_call
                read_call += 1
                if read_call == 1:
                    return b'{"type": "system", "session_id": "s1"}\n'
                # Simulate cancellation during second read
                raise asyncio.CancelledError()

            mock_proc.stdout = MagicMock()
            mock_proc.stdout.read = slow_read
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"")
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.terminate = MagicMock()
            mock_proc.kill = MagicMock()
            mock_exec.return_value = mock_proc

            with pytest.raises(asyncio.CancelledError):
                async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                    async for _ in stream:
                        pass

            # Verify cleanup was attempted
            mock_proc.terminate.assert_called()


# ---------------------------------------------------------------------------
# 4. Tool allowlist/blocklist: no spurious quotes in subprocess args
# ---------------------------------------------------------------------------


class TestToolAllowlistArgs:
    """Verify tool names are passed without embedded quotes to subprocess."""

    def test_allowed_tools_no_embedded_quotes(self):
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

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
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

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
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        request = ClaudeCodeRequest(
            prompt="test",
            mcp_config=".mcp/config.json",
        )
        args = request.as_cmd_args()

        idx = args.index("--mcp-config")
        config_val = args[idx + 1]
        assert '"' not in config_val, f"mcp_config value {config_val!r} has embedded quotes"


# ---------------------------------------------------------------------------
# 5. Mutable default: session must not be shared across calls
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    """Verify each stream_claude_code_cli call gets its own session."""

    def test_default_session_is_none_not_shared_instance(self):
        import inspect

        from lionagi.providers.anthropic.claude_code.models import (
            stream_claude_code_cli,
        )

        sig = inspect.signature(stream_claude_code_cli)
        session_param = sig.parameters["session"]
        assert session_param.default is None, (
            f"session default is {session_param.default!r}, not None — "
            "mutable default causes cross-request data leakage"
        )


# ---------------------------------------------------------------------------
# 6. Empty responses guard
# ---------------------------------------------------------------------------


class TestEmptyResponsesGuard:
    """Verify _call handles empty response list without IndexError."""

    @pytest.mark.asyncio
    async def test_auto_finish_with_empty_responses(self):
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        async def empty_stream(*args, **kwargs):
            return
            yield  # make it an async generator

        with patch(
            "lionagi.providers.anthropic.claude_code.endpoint.stream_claude_code_cli",
            side_effect=empty_stream,
        ):
            endpoint = ClaudeCodeCLIEndpoint()

            mock_request = MagicMock()
            mock_request.auto_finish = True
            mock_request.cli_include_summary = False

            # Must not raise IndexError on responses[-1]
            result = await endpoint._call({"request": mock_request}, {})
            assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 7. Process-group kill + non-Unix cleanup
# ---------------------------------------------------------------------------


def _make_mock_proc(pid=12345):
    """Build a minimal mock subprocess suitable for ndjson_from_cli tests."""
    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = AsyncMock(return_value=b"")
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"")
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    return mock_proc


class TestProcessGroupCleanup:
    """Gemini and Pi cleanup must use os.killpg on the process group.

    Claude and Codex capture the PGID immediately after spawn and call
    os.killpg(pgid, SIGTERM/SIGKILL) in cleanup so that descendant processes
    started by the CLI are also terminated.  Gemini and Pi now mirror this.
    """

    @pytest.mark.asyncio
    async def test_gemini_cleanup_calls_killpg(self):
        from lionagi.providers.google.gemini_code.models import (
            GeminiCodeRequest,
            _ndjson_from_cli,
        )

        request = GeminiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9001)
        killpg_calls: list[tuple] = []

        with (
            patch("lionagi.providers.google.gemini_code.models.GEMINI_CLI", "gemini"),
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

        # At least one killpg(9001, ...) call must have been made.
        assert any(pgid == 9001 for pgid, _ in killpg_calls), (
            f"Expected os.killpg(9001, ...) in cleanup; got {killpg_calls}. "
            "Gemini must terminate the whole process group."
        )

    @pytest.mark.asyncio
    async def test_pi_cleanup_calls_killpg(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest, _ndjson_from_cli

        request = PiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9002)
        killpg_calls: list[tuple] = []

        with (
            patch("lionagi.providers.pi.cli.models.PI_CLI", "pi"),
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

        assert any(pgid == 9002 for pgid, _ in killpg_calls), (
            f"Expected os.killpg(9002, ...) in cleanup; got {killpg_calls}. "
            "Pi must terminate the whole process group."
        )

    @pytest.mark.asyncio
    async def test_gemini_mock_pid_guard_skips_killpg(self):
        """pid=1 must be filtered by the safety guard, not passed to os.killpg."""
        from lionagi.providers.google.gemini_code.models import (
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
            patch("lionagi.providers.google.gemini_code.models.GEMINI_CLI", "gemini"),
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
    async def test_gemini_cleanup_no_killpg_platform(self, monkeypatch):
        """When os.killpg is absent (Windows), cleanup must fall through to proc.terminate()."""
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.google.gemini_code import models

        request = models.GeminiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9101)

        # Simulate Windows: os.killpg does not exist.
        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch("lionagi.providers.google.gemini_code.models.GEMINI_CLI", "gemini"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc

            # Must complete cleanly — no AttributeError leaking from cleanup.
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        # Group-kill skipped; direct terminate still ran.
        mock_proc.terminate.assert_called()

    @pytest.mark.asyncio
    async def test_pi_cleanup_no_killpg_platform(self, monkeypatch):
        """When os.killpg is absent (Windows), Pi cleanup must fall through to proc.terminate()."""
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.pi.cli import models

        request = models.PiCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9102)

        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch("lionagi.providers.pi.cli.models.PI_CLI", "pi"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc

            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        mock_proc.terminate.assert_called()


class TestKillpgUnavailablePlatform:
    """os.killpg is POSIX-only. On Windows a real pid still set _pgid, so the
    finally-block killpg call raised AttributeError (only ProcessLookupError /
    PermissionError suppressed), failing even a successful run. Cleanup must
    fall through to proc.terminate() instead."""

    @pytest.mark.asyncio
    async def test_claude_cleanup_no_killpg_platform(self, monkeypatch):
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.anthropic.claude_code import models

        request = models.ClaudeCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9201)
        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        mock_proc.terminate.assert_called()

    @pytest.mark.asyncio
    async def test_codex_cleanup_no_killpg_platform(self, monkeypatch):
        import lionagi.ln._proc as proc_mod
        from lionagi.providers.openai.codex import models

        request = models.CodexCodeRequest(prompt="test")
        mock_proc = _make_mock_proc(pid=9202)
        monkeypatch.delattr(proc_mod.os, "killpg", raising=False)

        with (
            patch("lionagi.providers.openai.codex.models.CODEX_CLI", "codex"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        mock_proc.terminate.assert_called()


# ---------------------------------------------------------------------------
# 8. Gemini & Pi stderr deadlock prevention
# ---------------------------------------------------------------------------


class TestStderrDeadlockPrevention:
    """Gemini and Pi drain stderr concurrently; a large payload must not deadlock."""

    @pytest.mark.asyncio
    async def test_gemini_large_stderr_does_not_deadlock(self):
        import asyncio

        from lionagi.providers.google.gemini_code.models import (
            GeminiCodeRequest,
            _ndjson_from_cli,
        )

        request = GeminiCodeRequest(prompt="test")
        large_stderr = b"E: " + b"x" * 65536  # 64 KB — fills a typical pipe buffer

        mock_proc = MagicMock()
        mock_proc.pid = 9003
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
        mock_proc.wait = AsyncMock(return_value=1)  # non-zero exit to test error path
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        with (
            patch("lionagi.providers.google.gemini_code.models.GEMINI_CLI", "gemini"),
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
    async def test_pi_large_stderr_does_not_deadlock(self):
        from lionagi.providers.pi.cli.models import PiCodeRequest, _ndjson_from_cli

        request = PiCodeRequest(prompt="test")
        large_stderr = b"E: " + b"y" * 65536

        mock_proc = MagicMock()
        mock_proc.pid = 9004
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
        mock_proc.wait = AsyncMock(return_value=1)
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        with (
            patch("lionagi.providers.pi.cli.models.PI_CLI", "pi"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("lionagi.ln._proc.os.killpg"),
        ):
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError) as exc_info:
                async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
                    async for _ in stream:
                        pass

        assert "y" * 10 in str(exc_info.value)
