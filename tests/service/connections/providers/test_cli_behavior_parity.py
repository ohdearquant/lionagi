"""Regression tests for per-provider CLI subprocess behaviour parity.

Covers: Claude tail repair, Codex double-workspace, Pi stdin inheritance,
Gemini/agy DEVNULL stdin (headless print mode).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Claude EOF JSON repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ndjson_from_cli_tail_repair_yields_repaired_object():
    """tail_repair callback is invoked on a malformed final JSON fragment and
    the repaired dict is yielded rather than silently dropped."""
    from lionagi.providers._cli_subprocess import ndjson_from_cli

    malformed = b'{"type": "done", "missing_close":'

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.pid = 2  # >1 so the PGID guard passes
        proc.stdout = AsyncMock()
        # First read returns the malformed bytes; second signals EOF.
        proc.stdout.read = AsyncMock(side_effect=[malformed, b""])
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    repaired_obj = {"type": "done", "missing_close": None}

    def _repair(buf: str) -> dict | None:
        return repaired_obj

    collected = []
    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        async with __import__("contextlib").aclosing(
            ndjson_from_cli(["fake-cli"], tail_repair=_repair)
        ) as stream:
            async for obj in stream:
                collected.append(obj)

    assert collected == [repaired_obj], (
        "tail_repair should yield the repaired object when raw_decode fails on the tail"
    )


@pytest.mark.asyncio
async def test_ndjson_from_cli_no_tail_repair_drops_bad_tail(caplog):
    """Without tail_repair the malformed tail is logged and dropped (default)."""
    import logging

    from lionagi.providers._cli_subprocess import ndjson_from_cli

    malformed = b'{"type": "bad"'

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.pid = 2
        proc.stdout = AsyncMock()
        proc.stdout.read = AsyncMock(side_effect=[malformed, b""])
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    collected = []
    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        with caplog.at_level(logging.ERROR):
            async with __import__("contextlib").aclosing(ndjson_from_cli(["fake-cli"])) as stream:
                async for obj in stream:
                    collected.append(obj)

    assert collected == [], "No objects should be yielded for an unrecoverable tail"
    assert any("Skipped unrecoverable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_claude_ndjson_uses_repair_callback():
    """_ndjson_from_cli in Claude models passes tail_repair to ndjson_from_cli."""
    from lionagi.providers.anthropic import claude_code as cc_models
    from lionagi.providers.anthropic import claude_code as cc_pkg

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield  # make it an async generator

    with (
        patch.object(cc_models, "CLAUDE_CLI", "/fake/claude"),
        patch.object(cc_models, "ndjson_from_cli", _fake_ndjson),
    ):
        req = cc_models.ClaudeCodeRequest(prompt="hi")
        workspace = req.cwd()
        workspace.mkdir(parents=True, exist_ok=True)
        async for _ in cc_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    kw = call_kwargs[0]
    assert "tail_repair" in kw, "Claude must pass tail_repair to ndjson_from_cli"
    assert kw["tail_repair"] is not None, "tail_repair must not be None for Claude"


# ---------------------------------------------------------------------------
# Codex does not double-apply workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_ndjson_does_not_pass_cwd():
    """Codex _ndjson_from_cli must NOT pass cwd= to ndjson_from_cli.

    The Codex CLI already receives the workspace via '-C <repo>' in as_cmd_args().
    Passing cwd= as well causes a relative repo path to be resolved twice.
    """
    from lionagi.providers.openai import codex as codex_models

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield

    with (
        patch.object(codex_models, "CODEX_CLI", "/fake/codex"),
        patch.object(codex_models, "ndjson_from_cli", _fake_ndjson),
    ):
        req = codex_models.CodexCodeRequest(prompt="p", repo=Path("relative_repo"))
        async for _ in codex_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    kw = call_kwargs[0]
    assert "cwd" not in kw or kw.get("cwd") is None, (
        "Codex must not pass cwd= to ndjson_from_cli (workspace is set via -C in argv)"
    )


def test_codex_as_cmd_args_contains_dash_c_flag():
    """Codex as_cmd_args() emits '-C <repo>' so the CLI handles the workspace itself."""
    from lionagi.providers.openai.codex import CodexCodeRequest

    req = CodexCodeRequest(prompt="hello", repo=Path("my_repo"))
    args = req.as_cmd_args()

    assert "-C" in args
    c_idx = args.index("-C")
    assert args[c_idx + 1] == "my_repo"


# ---------------------------------------------------------------------------
# Pi inherits stdin; Gemini (agy print mode) keeps the DEVNULL default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_ndjson_defaults_stdin_devnull():
    """agy runs headless print mode and reads nothing from stdin — gemini must
    NOT pass a stdin override, leaving ndjson_from_cli's DEVNULL default."""
    from lionagi.providers.google import gemini_code as gemini_models

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield

    with (
        patch.object(gemini_models, "AGY_CLI", "/fake/agy"),
        patch.object(gemini_models, "ndjson_from_cli", _fake_ndjson),
    ):
        req = gemini_models.GeminiCodeRequest(prompt="hello")
        workspace = req.cwd()
        workspace.mkdir(parents=True, exist_ok=True)
        async for _ in gemini_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    assert "stdin" not in call_kwargs[0], (
        "agy print mode must not override stdin (DEVNULL default applies)"
    )


@pytest.mark.asyncio
async def test_pi_ndjson_passes_inherit_stdin():
    """Pi _ndjson_from_cli must pass stdin=_INHERIT_STDIN to ndjson_from_cli."""
    from lionagi.providers._cli_subprocess import _INHERIT_STDIN
    from lionagi.providers.pi import cli as pi_models

    call_kwargs: list[dict] = []

    async def _fake_ndjson(cmd, **kwargs):
        call_kwargs.append(kwargs)
        return
        yield

    with (
        patch.object(pi_models, "PI_CLI", "/fake/pi"),
        patch.object(pi_models, "ndjson_from_cli", _fake_ndjson),
    ):
        req = pi_models.PiCodeRequest(prompt="hello")
        async for _ in pi_models._ndjson_from_cli(req):
            pass

    assert call_kwargs, "ndjson_from_cli must be called"
    kw = call_kwargs[0]
    assert kw.get("stdin") is _INHERIT_STDIN, (
        "Pi must pass stdin=_INHERIT_STDIN so the child inherits the parent stdin"
    )


@pytest.mark.asyncio
async def test_ndjson_from_cli_inherit_stdin_omits_stdin_kwarg():
    """When stdin=_INHERIT_STDIN the helper must NOT pass stdin to create_subprocess_exec."""
    from lionagi.providers._cli_subprocess import _INHERIT_STDIN, ndjson_from_cli

    captured_kwargs: list[dict] = []

    async def _fake_exec(*args, **kwargs):
        captured_kwargs.append(kwargs)
        proc = MagicMock()
        proc.pid = 2
        proc.stdout = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"")
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        async with __import__("contextlib").aclosing(
            ndjson_from_cli(["fake-cli"], stdin=_INHERIT_STDIN)
        ) as stream:
            async for _ in stream:
                pass

    assert captured_kwargs, "create_subprocess_exec must be called"
    kw = captured_kwargs[0]
    assert "stdin" not in kw, (
        "_INHERIT_STDIN sentinel must cause ndjson_from_cli to omit stdin "
        "from create_subprocess_exec, so the child inherits parent stdin"
    )


@pytest.mark.asyncio
async def test_ndjson_from_cli_devnull_passes_stdin_devnull():
    """Default stdin=DEVNULL must be forwarded to create_subprocess_exec (Claude/Codex)."""
    from lionagi.providers._cli_subprocess import ndjson_from_cli

    captured_kwargs: list[dict] = []

    async def _fake_exec(*args, **kwargs):
        captured_kwargs.append(kwargs)
        proc = MagicMock()
        proc.pid = 2
        proc.stdout = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"")
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        async with __import__("contextlib").aclosing(ndjson_from_cli(["fake-cli"])) as stream:
            async for _ in stream:
                pass

    assert captured_kwargs
    kw = captured_kwargs[0]
    assert kw.get("stdin") == asyncio.subprocess.DEVNULL, (
        "Default behaviour (DEVNULL) must be forwarded to create_subprocess_exec"
    )
