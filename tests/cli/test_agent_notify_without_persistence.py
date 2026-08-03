# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A run that asked for a terminal notice, and lost its persistence before it
could register the callback that would normally deliver one, still gets it
delivered.

`--notify` is ordinarily delivered by a callback registered against this
run's session entity and fired by that entity's terminal transition. When
persistence setup fails there is no session entity, so no transition can ever
happen and the registered path can never fire, however well it resolves. This
run instead delivers the notice itself, directly, once its own terminal
status is known — see `deliver_flow_notify_now` in
`lionagi/cli/orchestrate/_notify.py` and docs/internals/cli.md.

That matters because the consumers are automated. The lion MCP server wires
`--notify` on every job it spawns and takes the resulting notice as the run's
end; without one it eventually observes the process is gone with nothing
recorded and publishes `outcome=indeterminate`, which is the opposite of what
happened to a run that did all of its work.

Recording a refusal is not the same as delivering a notice: the refusal
record (`notify_outcome.json` with a `reason`) is now written only when
delivery is actually attempted and genuinely cannot be completed — nothing
usable is configured — never merely because persistence broke.
"""

from __future__ import annotations

import json
import sys
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
async def test_notify_unusable_config_without_a_session_records_the_refusal(monkeypatch, tmp_path):
    """A notifier that was asked for and cannot even be resolved is refused
    and recorded — the only case that still writes a bare refusal now that
    delivery is attempted directly."""
    run = _wire_agent_stubs(monkeypatch, tmp_path, persist=None)

    from lionagi.cli.agent import _run_agent

    # Shell features are never honored by any notify resolver; this is
    # rejected before any delivery is attempted.
    await _run_agent("codex/model", "do the thing", notify="echo hi | cat")

    assert run.notify_outcome_path.exists(), (
        "a notifier was asked for and could not even be resolved; the run has to record that"
    )
    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome["ok"] is False
    assert outcome["reason"] == "on_terminal_command_requires_shell_features"
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


def _write_fake_notifier(tmp_path: Path) -> Path:
    """A tiny script standing in for a real `--notify` adapter: argv[1] is
    where it records that it ran, argv[2] (if given) is written into it — the
    `{status}` placeholder, in the delivery test below, so the assertion is
    on what the CLI actually substituted, not just that something ran."""
    script = tmp_path / "fake_notifier.py"
    script.write_text(
        "import sys\n"
        "path = sys.argv[1]\n"
        "status = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "with open(path, 'w') as f:\n"
        "    f.write(status)\n"
    )
    return script


@pytest.mark.asyncio
async def test_direct_path_delivers_the_notice_when_persistence_is_lost(monkeypatch, tmp_path):
    """The reproduced condition: persistence setup fails for a run submitted
    with a notify template, and the notice is DELIVERED — the fake delivery
    command records its own invocation, carrying the run's real terminal
    status, not silence and not a bare refusal record."""
    run = _wire_agent_stubs(monkeypatch, tmp_path, persist=None)
    marker = tmp_path / "delivered.txt"
    script = _write_fake_notifier(tmp_path)
    notify_cmd = f"{sys.executable} {script} {marker} {{status}}"

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/model", "do the thing", notify=notify_cmd)

    assert marker.exists(), "the fake delivery command was never invoked"
    assert marker.read_text() == "completed"


@pytest.mark.asyncio
async def test_control_disabling_the_direct_path_delivers_nothing(monkeypatch, tmp_path):
    """The pre-fix behaviour, reproduced as a control: with the direct-path
    seam disabled, the identical arrangement above delivers nothing. This is
    what proves the test above exercises the new code, not something that
    would have passed anyway (a fake notifier that always runs, a marker file
    that already existed, etc.)."""
    run = _wire_agent_stubs(monkeypatch, tmp_path, persist=None)
    marker = tmp_path / "delivered.txt"
    script = _write_fake_notifier(tmp_path)
    notify_cmd = f"{sys.executable} {script} {marker} {{status}}"

    import lionagi.cli.orchestrate._notify as notify_mod

    async def _disabled(*args, **kwargs):
        return None

    monkeypatch.setattr(notify_mod, "deliver_flow_notify_now", _disabled)

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/model", "do the thing", notify=notify_cmd)

    assert not marker.exists(), "the old (pre-fix) path must not deliver anything"
    assert not run.notify_outcome_path.exists(), (
        "the old path recorded nothing either — this is what made the run look "
        "exactly like one that never asked for a notifier at all"
    )
