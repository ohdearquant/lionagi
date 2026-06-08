# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for li agent codex robustness fixes — issues #1158, #1152, #1154.

#1158: naked model spec fails without --bypass; -a profile works
#1152: li agent timeout discards all partial output
#1154: li agent codex: add timeout and progress tracking for long-running agents
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.cli._providers import (
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_YOLO_KWARGS,
    build_chat_model,
)

# ── #1158: build_chat_model threads bypass kwarg ──────────────────────────────


def test_build_chat_model_bypass_applies_bypass_kwargs(monkeypatch):
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_chat_model(
        "codex", "gpt-5.3-codex-spark", yolo=False, verbose=False, theme=None, bypass=True
    )

    assert len(captor.captures) == 1
    kwargs = captor.captures[0]
    expected = PROVIDER_BYPASS_KWARGS["codex"]
    for k, v in expected.items():
        assert kwargs.get(k) == v, f"Expected {k}={v!r} in kwargs, got {kwargs.get(k)!r}"


def test_build_chat_model_bypass_takes_precedence_over_yolo(monkeypatch):
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_chat_model(
        "codex", "gpt-5.3-codex-spark", yolo=True, verbose=False, theme=None, bypass=True
    )

    assert len(captor.captures) == 1
    kwargs = captor.captures[0]
    # bypass_approvals from PROVIDER_BYPASS_KWARGS (not full_auto from PROVIDER_YOLO_KWARGS)
    assert kwargs.get("bypass_approvals") is True
    assert "full_auto" not in kwargs, "full_auto (yolo) must not override bypass"


def test_build_chat_model_no_bypass_no_yolo_returns_str_for_codex():
    result = build_chat_model("codex", "gpt-5.3-codex-spark", yolo=False, verbose=False, theme=None)
    assert isinstance(result, str)
    assert result == "codex/gpt-5.3-codex-spark"


def test_build_chat_model_bypass_claude_applies_permission_mode(monkeypatch):
    import lionagi.cli._providers as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_chat_model("claude", "sonnet", yolo=False, verbose=False, theme=None, bypass=True)

    assert len(captor.captures) == 1
    kwargs = captor.captures[0]
    assert kwargs.get("permission_mode") == "bypassPermissions"


# ── #1158: _run_agent threads bypass and warns for naked codex ────────────────


def _make_agent_mocks_with_bypass(monkeypatch, tmp_path, captured_kwargs: list):
    """Wire all external stubs for _run_agent; spy on build_chat_model kwargs."""

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    async def spy_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", spy_operate)

    from lionagi.service.manager import iModelManager

    async def fake_shutdown(self):
        pass

    monkeypatch.setattr(iModelManager, "shutdown", fake_shutdown)

    def spy_build_chat_model(*args, **kwargs):
        # args: provider, model, yolo, verbose, theme, effort, fast, bypass
        captured_kwargs.append({"args": args, "kwargs": kwargs})
        return "codex/gpt-5.3-codex-spark"

    monkeypatch.setattr(agent_mod, "build_chat_model", spy_build_chat_model)
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)

    fake_run = SimpleNamespace(
        run_id="test-run",
        artifact_root=tmp_path / "artifacts",
        stream_dir=tmp_path / "stream",
        branches_dir=tmp_path / "branches",
    )
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: fake_run)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc123",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)


@pytest.mark.asyncio
async def test_run_agent_threads_bypass_to_build_chat_model(monkeypatch, tmp_path):
    captured: list = []
    _make_agent_mocks_with_bypass(monkeypatch, tmp_path, captured)

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/gpt-5.3-codex-spark", "do stuff", bypass=True)

    assert captured, "build_chat_model was never called"
    call = captured[0]
    # bypass is the 8th positional arg (index 7): provider, model, yolo, verbose, theme, effort, fast, bypass
    args = call["args"]
    assert len(args) >= 8, f"Expected ≥8 positional args, got {len(args)}: {args}"
    assert args[7] is True, f"bypass arg (index 7) expected True, got {args[7]!r}"


@pytest.mark.asyncio
async def test_run_agent_codex_no_bypass_emits_warning(monkeypatch, tmp_path, capsys):
    captured: list = []
    _make_agent_mocks_with_bypass(monkeypatch, tmp_path, captured)

    warnings_emitted: list[str] = []

    import lionagi.cli.agent as agent_mod

    original_warn = None
    import lionagi.cli._logging as logging_mod

    original_warn = logging_mod.warn

    def capture_warn(msg):
        warnings_emitted.append(msg)

    monkeypatch.setattr(logging_mod, "warn", capture_warn)
    # Also patch via agent_mod's import
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/gpt-5.3-codex-spark")

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/gpt-5.3-codex-spark", "do stuff", bypass=False, yolo=False)

    assert any("--bypass" in w or "bypass" in w.lower() for w in warnings_emitted), (
        f"Expected a bypass warning, got: {warnings_emitted}"
    )


@pytest.mark.asyncio
async def test_run_agent_codex_with_bypass_no_warning(monkeypatch, tmp_path):
    captured: list = []
    _make_agent_mocks_with_bypass(monkeypatch, tmp_path, captured)

    warnings_emitted: list[str] = []
    import lionagi.cli._logging as logging_mod

    monkeypatch.setattr(logging_mod, "warn", lambda msg: warnings_emitted.append(msg))

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/gpt-5.3-codex-spark")

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/gpt-5.3-codex-spark", "do stuff", bypass=True, yolo=False)

    bypass_warns = [w for w in warnings_emitted if "bypass" in w.lower() and "require" in w.lower()]
    assert not bypass_warns, f"Unexpected bypass warning when bypass=True: {bypass_warns}"


@pytest.mark.asyncio
async def test_run_agent_codex_with_yolo_no_warning(monkeypatch, tmp_path):
    captured: list = []
    _make_agent_mocks_with_bypass(monkeypatch, tmp_path, captured)

    warnings_emitted: list[str] = []
    import lionagi.cli._logging as logging_mod

    monkeypatch.setattr(logging_mod, "warn", lambda msg: warnings_emitted.append(msg))

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/gpt-5.3-codex-spark")

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/gpt-5.3-codex-spark", "do stuff", bypass=False, yolo=True)

    bypass_req_warns = [
        w for w in warnings_emitted if "require" in w.lower() and "bypass" in w.lower()
    ]
    assert not bypass_req_warns, f"Unexpected warning with yolo=True: {bypass_req_warns}"


# ── #1152: partial output preserved on timeout ────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_timeout_preserves_partial_output(monkeypatch, tmp_path):

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    # Simulate a branch that has accumulated a partial assistant message
    _partial_text = "Partial review: file A looks fine, file B has an issue…"

    async def timeout_operate(self, instruction=None, **kw):
        from lionagi._errors import TimeoutError as LionTimeoutError

        raise LionTimeoutError("timed out")

    monkeypatch.setattr(Branch, "operate", timeout_operate)

    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())

    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/gpt-5.3-codex-spark")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)

    fake_run = SimpleNamespace(
        run_id="test-run",
        artifact_root=tmp_path / "artifacts",
        stream_dir=tmp_path / "stream",
        branches_dir=tmp_path / "branches",
    )
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: fake_run)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc123",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)

    # Make the branch's last_response yield our fake partial text
    from lionagi.protocols.messages.manager import MessageManager

    monkeypatch.setattr(
        MessageManager,
        "last_response",
        property(lambda self: SimpleNamespace(response=_partial_text)),
    )

    from lionagi.cli.agent import _run_agent

    result, _provider, _branch_id, terminal_status = await _run_agent(
        "codex/gpt-5.3-codex-spark",
        "review all files",
        timeout=30,
        bypass=True,
    )

    assert terminal_status == "timed_out"
    assert result == _partial_text, (
        f"Expected partial output to be preserved on timeout, got: {result!r}"
    )


@pytest.mark.asyncio
async def test_run_agent_timeout_empty_partial_returns_empty_string(monkeypatch, tmp_path):

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    async def timeout_operate(self, instruction=None, **kw):
        from lionagi._errors import TimeoutError as LionTimeoutError

        raise LionTimeoutError("timed out")

    monkeypatch.setattr(Branch, "operate", timeout_operate)

    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/gpt-5.3-codex-spark")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc123",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "a",
            stream_dir=tmp_path / "s",
            branches_dir=tmp_path / "b",
        ),
    )
    # No partial output
    from lionagi.protocols.messages.manager import MessageManager

    monkeypatch.setattr(MessageManager, "last_response", property(lambda self: None))

    from lionagi.cli.agent import _run_agent

    result, _provider, _branch_id, terminal_status = await _run_agent(
        "codex/gpt-5.3-codex-spark",
        "review all files",
        timeout=30,
        bypass=True,
    )

    assert terminal_status == "timed_out"
    assert result == "", f"Expected empty string, got: {result!r}"


# ── #1154: progress heartbeat fires during timeout runs ───────────────────────


@pytest.mark.asyncio
async def test_run_agent_heartbeat_started_when_timeout_set(monkeypatch, tmp_path):

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    heartbeat_tasks_created: list = []
    original_ensure_future = asyncio.ensure_future

    def spy_ensure_future(coro, **kw):
        task = original_ensure_future(coro, **kw)
        heartbeat_tasks_created.append(task)
        return task

    monkeypatch.setattr(asyncio, "ensure_future", spy_ensure_future)

    async def fast_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fast_operate)

    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/gpt-5.3-codex-spark")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}", agent_definition_hash=lambda n: "h"
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "a",
            stream_dir=tmp_path / "s",
            branches_dir=tmp_path / "b",
        ),
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/gpt-5.3-codex-spark", "do stuff", timeout=300, bypass=True)

    assert heartbeat_tasks_created, (
        "Expected at least one heartbeat task to be created when timeout is set"
    )


@pytest.mark.asyncio
async def test_run_agent_no_heartbeat_when_timeout_none(monkeypatch, tmp_path):

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    heartbeat_tasks_created: list = []
    original_ensure_future = asyncio.ensure_future

    def spy_ensure_future(coro, **kw):
        task = original_ensure_future(coro, **kw)
        heartbeat_tasks_created.append(task)
        return task

    monkeypatch.setattr(asyncio, "ensure_future", spy_ensure_future)

    async def fast_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fast_operate)

    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/gpt-5.3-codex-spark")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}", agent_definition_hash=lambda n: "h"
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "a",
            stream_dir=tmp_path / "s",
            branches_dir=tmp_path / "b",
        ),
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/gpt-5.3-codex-spark", "do stuff", timeout=None, bypass=True)

    assert not heartbeat_tasks_created, (
        f"Expected no heartbeat task when timeout=None, got {len(heartbeat_tasks_created)}"
    )
