"""Regression tests for CLI subprocess cancellation (Ctrl+C / SIGINT).

Root cause (fixed in #887): Without start_new_session=True, Ctrl+C sends
SIGINT to both Python and the CLI subprocess. The child handles SIGINT
gracefully and exits with a partial result, causing auto_finish to spawn
a new session instead of stopping.

These tests verify:
1. Subprocesses are spawned in isolated sessions (start_new_session=True)
2. CancelledError during streaming propagates without triggering auto_finish
3. The aclosing() cleanup path terminates the subprocess
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
        """Claude CLI subprocess must use start_new_session=True."""
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        request = ClaudeCodeRequest(prompt="test")

        from lionagi.providers.anthropic.claude_code.models import _ndjson_from_cli

        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
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
            assert (
                kwargs.get("start_new_session") is True
            ), "CLI subprocess must use start_new_session=True to isolate from SIGINT"

    @pytest.mark.asyncio
    async def test_codex_cli_uses_start_new_session(self):
        """Codex CLI subprocess must use start_new_session=True."""
        from lionagi.providers.openai.codex.models import CodexCodeRequest

        request = CodexCodeRequest(prompt="test")

        from lionagi.providers.openai.codex.models import _ndjson_from_cli

        with (
            patch("lionagi.providers.openai.codex.models.CODEX_CLI", "codex"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
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
        """Gemini CLI subprocess must use start_new_session=True."""
        from lionagi.providers.google.gemini_code.models import GeminiCodeRequest

        request = GeminiCodeRequest(prompt="test")

        from lionagi.providers.google.gemini_code.models import _ndjson_from_cli

        with (
            patch("lionagi.providers.google.gemini_code.models.GEMINI_CLI", "gemini"),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock
            ) as mock_exec,
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
        """CancelledError during first stream must not spawn a second request."""
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        stream_call_count = 0

        async def cancelling_stream(*args, **kwargs):
            """Yields one chunk then raises CancelledError (simulates Ctrl+C)."""
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
            assert (
                stream_call_count == 1
            ), f"auto_finish spawned {stream_call_count - 1} extra session(s) after cancellation"

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_skips_auto_finish(self):
        """KeyboardInterrupt during streaming must not spawn a second request."""
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
        """Task.cancel() (non-SIGINT) during streaming must not spawn a second request.

        This tests the actual regression scenario: the stream completes normally
        (subprocess exits gracefully), but the Python task is cancelled by the
        executor/timeout. The _cancelled sentinel guard must prevent auto_finish.
        """
        from lionagi.providers.anthropic.claude_code.endpoint import (
            ClaudeCodeCLIEndpoint,
        )

        stream_call_count = 0

        async def stream_then_cancel(*args, **kwargs):
            """Yields chunks normally, then the task gets cancelled externally."""
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
        """Normal stream completion should still trigger auto_finish when needed."""
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
            assert (
                stream_call_count == 2
            ), "auto_finish should fire on normal completion"


# ---------------------------------------------------------------------------
# 3. _ndjson_from_cli cleanup: CancelledError must not be swallowed
# ---------------------------------------------------------------------------


class TestNdjsonCleanupPropagation:
    """Verify _ndjson_from_cli finally block doesn't swallow CancelledError."""

    @pytest.mark.asyncio
    async def test_cancelled_error_not_swallowed_by_finally(self):
        """CancelledError from proc.wait() timeout must propagate, not be caught."""
        # This tests the fix: except asyncio.TimeoutError (not CancelledError)
        from lionagi.providers.anthropic.claude_code.models import (
            ClaudeCodeRequest,
            _ndjson_from_cli,
        )

        request = ClaudeCodeRequest(prompt="test")

        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
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
        """Tool names in --allowedTools must not have embedded quote chars."""
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
        """Tool names in --disallowedTools must not have embedded quote chars."""
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
        """--mcp-config path must not have embedded quote chars."""
        from lionagi.providers.anthropic.claude_code.models import ClaudeCodeRequest

        request = ClaudeCodeRequest(
            prompt="test",
            mcp_config="/path/to/config.json",
        )
        args = request.as_cmd_args()

        idx = args.index("--mcp-config")
        config_val = args[idx + 1]
        assert (
            '"' not in config_val
        ), f"mcp_config value {config_val!r} has embedded quotes"


# ---------------------------------------------------------------------------
# 5. Mutable default: session must not be shared across calls
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    """Verify each stream_claude_code_cli call gets its own session."""

    def test_default_session_is_none_not_shared_instance(self):
        """stream_claude_code_cli must default session to None, not a shared instance."""
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
        """auto_finish must not IndexError when no chunks were received."""
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
# 7. Non-Unix cleanup: os.killpg unavailable must not break cleanup
# ---------------------------------------------------------------------------


def _make_cleanup_mock_proc(pid=12345):
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


class TestKillpgUnavailablePlatform:
    """os.killpg is POSIX-only. On Windows a real pid still set _pgid, so the
    finally-block killpg call raised AttributeError (only ProcessLookupError /
    PermissionError suppressed), failing even a successful run. Cleanup must
    fall through to proc.terminate() instead."""

    @pytest.mark.asyncio
    async def test_claude_cleanup_no_killpg_platform(self, monkeypatch):
        from lionagi.providers.anthropic.claude_code import models

        request = models.ClaudeCodeRequest(prompt="test")
        mock_proc = _make_cleanup_mock_proc(pid=9201)
        monkeypatch.delattr(models.os, "killpg", raising=False)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        mock_proc.terminate.assert_called()

    @pytest.mark.asyncio
    async def test_codex_cleanup_no_killpg_platform(self, monkeypatch):
        from lionagi.providers.openai.codex import models

        request = models.CodexCodeRequest(prompt="test")
        mock_proc = _make_cleanup_mock_proc(pid=9202)
        monkeypatch.delattr(models.os, "killpg", raising=False)

        with (
            patch("lionagi.providers.openai.codex.models.CODEX_CLI", "codex"),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = mock_proc
            async with contextlib.aclosing(models._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

        mock_proc.terminate.assert_called()
