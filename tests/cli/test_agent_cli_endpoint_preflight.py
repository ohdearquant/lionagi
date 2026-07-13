# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent <model> <prompt>` with a non-CLI provider (e.g. a bare
'gpt-5.3-codex-spark' instead of 'codex/gpt-5.3-codex-spark') must fail fast,
before any run is allocated or persisted — instead of allocating a run,
persisting a session, and only then failing deep inside operations/run/run.py
once the turn is already streaming, which records a spurious reliability
failure for what is actually a CLI usage error.

Covers the preflight guard wired into `_run_agent` right before
`allocate_run()`.
"""

from __future__ import annotations

import pytest

from lionagi._errors import ConfigurationError

# ---------------------------------------------------------------------------
# _run_agent: fails before any spawn / run allocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_rejects_non_cli_provider_before_any_spawn(monkeypatch):
    """A model spec resolving to a non-CLI provider must raise before
    allocate_run/setup_agent_persist ever run — i.e. before any run record
    could be created."""
    import lionagi.cli.agent as agent_mod

    def _boom_allocate_run():
        raise AssertionError(
            "allocate_run must not be reached — CLI-endpoint validation must fire first"
        )

    monkeypatch.setattr(agent_mod, "allocate_run", _boom_allocate_run)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError) as exc_info:
        await _run_agent("openai/gpt-4.1-mini", "do the thing")

    msg = str(exc_info.value)
    assert "only supports CLI endpoints" in msg
    assert "openai" in msg


@pytest.mark.asyncio
async def test_run_agent_non_cli_provider_message_names_cli_prefixes(monkeypatch):
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: (_ for _ in ()).throw(AssertionError("must not reach allocate_run")),
    )

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError) as exc_info:
        await _run_agent("openai/gpt-4.1-mini", "do the thing")

    msg = str(exc_info.value)
    for prefix in ("claude_code", "codex", "gemini-cli", "pi"):
        assert prefix in msg


@pytest.mark.asyncio
async def test_run_agent_accepts_cli_provider_and_reaches_allocate_run(monkeypatch, tmp_path):
    """Regression guard: a genuine CLI-backed model must not be rejected —
    the preflight only fires for a non-CLI endpoint."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    reached = {"allocate_run": False}

    def _fake_allocate_run():
        reached["allocate_run"] = True
        return SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        )

    monkeypatch.setattr(agent_mod, "allocate_run", _fake_allocate_run)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", **kw):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)

    async def fake_operate(self, instruction=None, **kw):
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)

    _result, _provider, _bid, terminal_status, _sid = await agent_mod._run_agent(
        "codex/model", "do the thing"
    )

    assert terminal_status == "completed"
    assert reached["allocate_run"] is True
