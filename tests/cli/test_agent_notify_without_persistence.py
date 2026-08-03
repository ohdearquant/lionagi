# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A run that asked for a terminal notice it can never get has to say so.

`--notify` is delivered by a callback registered against this run's session
entity and fired by that entity's terminal transition. When persistence setup
fails there is no session entity, so no transition can happen and the adapter
is never called, however well it resolves. The run then finishes its work and
ends in silence, and silence is exactly what a run that never asked for a
notifier produces.

That matters because the consumers are automated. The lion MCP server wires
`--notify` on every job it spawns and takes the resulting notice as the run's
end; without one it eventually observes the process is gone with nothing
recorded and publishes `outcome=indeterminate`, which is the opposite of what
happened to a run that did all of its work.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _wire_agent_stubs(monkeypatch, tmp_path: Path, *, persist: dict | None):
    """Stub _run_agent's external I/O, keeping a REAL run directory.

    The run directory is the one thing not stubbed here: the refusal record is
    a file this code writes into it, so a SimpleNamespace stand-in would make
    the assertion about the stand-in. *persist* is what setup_agent_persist
    returns, None being the failure this module is about.
    """
    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.cli._runs import allocate_run
    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return persist

    async def fake_teardown(
        ctx,
        *,
        status="completed",
        exception=None,
        cwd=None,
        engine_session_uid=None,
        defer_terminal=False,
    ):
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

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="notify-without-persistence")
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: run)

    async def fake_operate(self, instruction=None, **kw):
        return "the work this run was asked to do"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    return run


@pytest.mark.asyncio
async def test_notify_asked_for_without_a_session_records_the_refusal(monkeypatch, tmp_path):
    """No session entity to fire on is recorded, not passed over in silence."""
    run = _wire_agent_stubs(monkeypatch, tmp_path, persist=None)

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/model", "do the thing", notify="/some/notifier")

    assert run.notify_outcome_path.exists(), (
        "a notifier was asked for and can never fire; the run has to record that"
    )
    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome["ok"] is False
    # The reason is what separates this from every other way a notice fails to
    # arrive, so it is asserted by value rather than by being present.
    assert outcome["reason"] == "run_has_no_persisted_session_to_notify_on"
    # Nothing was launched, so there is no exit code and no captured stderr to
    # point at. Asserting these keeps the record from growing a fabricated
    # success shape later.
    assert outcome["exit_code"] is None
    assert outcome["stderr_path"] is None


@pytest.mark.asyncio
async def test_no_notifier_asked_for_writes_no_refusal(monkeypatch, tmp_path):
    """The control that stops the record from meaning nothing.

    Without this, a change that wrote the refusal unconditionally would pass
    the test above while reporting a refused notifier on every run that never
    wanted one, and the field would stop distinguishing anything.
    """
    run = _wire_agent_stubs(monkeypatch, tmp_path, persist=None)

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/model", "do the thing")

    assert not run.notify_outcome_path.exists(), (
        "a run that asked for nothing must look different from one that was refused"
    )
