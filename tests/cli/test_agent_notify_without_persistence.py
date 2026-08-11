# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A run that asked for a terminal notice, and lost its persistence before it
could register the callback that would normally deliver one, still gets it
delivered.

`--notify` is ordinarily delivered by a callback registered against the run's
session entity, fired by that entity's terminal transition. When persistence
setup fails there is no session entity and no transition ever fires, so this
run instead delivers the notice itself once its own terminal status is known
— see `deliver_flow_notify_now` in `lionagi/cli/orchestrate/_notify.py` and
docs/internals/cli.md.

This matters because consumers are automated: the lion MCP server wires
`--notify` on every job it spawns and takes the notice as the run's end;
without one it eventually observes the process gone with nothing recorded and
publishes `outcome=indeterminate` for a run that actually completed its work.

Recording a refusal is not the same as delivering a notice: the refusal
record (`notify_outcome.json` with a `reason`) is written only when delivery
is actually attempted and genuinely cannot complete, never merely because
persistence broke.
"""

from __future__ import annotations

import json
import logging
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


def _write_appending_notifier(tmp_path: Path) -> Path:
    """Like the fake notifier above, but appends.

    The question in the two tests below is how MANY notices one logical run
    sends, so a notifier that overwrites would report a two-notice run and a
    one-notice run identically — and the wrong count is the defect.
    """
    script = tmp_path / "appending_notifier.py"
    script.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\n")
    return script


@pytest.mark.asyncio
async def test_a_leg_that_will_auto_resume_leaves_the_notice_to_the_resumed_leg(
    monkeypatch, tmp_path
):
    """A timed-out leg that is about to auto-resume has no answer yet.

    Its status is `timed_out` and the run is not over: the recursion below it
    carries on and can finish the work. Delivering from the interim leg told
    the notifier the run timed out while the run went on to complete, and the
    notifier's whole job is to report how the run ended.

    Both legs are real here, including the recursion and the per-leg run
    allocation. Pinning them to one run directory would hide the count behind
    the once-per-run delivery guard, which is a different mechanism from the
    one under test.
    """
    _wire_agent_stubs(monkeypatch, tmp_path, persist=None)

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi._errors import TimeoutError as LionTimeoutError
    from lionagi.cli._runs import allocate_run as real_allocate_run

    monkeypatch.setattr(agent_mod, "allocate_run", real_allocate_run)

    marker = tmp_path / "notices.txt"
    script = _write_appending_notifier(tmp_path)

    turns = {"n": 0}

    async def operate(self, instruction=None, **kw):
        turns["n"] += 1
        if turns["n"] == 1:
            raise LionTimeoutError("synthetic")
        return "the resumed leg concluded the task"

    monkeypatch.setattr(Branch, "operate", operate)
    branch_file = tmp_path / "branch.json"
    branch_file.write_text("{}")
    monkeypatch.setattr(agent_mod, "find_branch", lambda rid: (None, branch_file))
    monkeypatch.setattr(Branch, "from_dict", classmethod(lambda cls, data: Branch()))

    result = await agent_mod._run_agent(
        "codex/model",
        "do the thing",
        timeout=1,
        resume_on_timeout=True,
        notify=f"{sys.executable} {script} {marker} {{status}}",
    )

    assert turns["n"] == 2, "the resume never happened, so this asserts nothing about it"
    notices = marker.read_text().split() if marker.exists() else []
    assert notices == ["completed"], (
        f"one notice, carrying the status the run actually reached; got {notices}"
    )
    assert result[3] == "completed"


@pytest.mark.asyncio
async def test_an_empty_resumed_stream_notifies_the_status_it_is_converted_to(
    monkeypatch, tmp_path
):
    """A resume that produces nothing is converted from `completed` to
    `failed`, and that conversion happens after the teardown that used to
    deliver. So the notifier was told `completed` about a run whose own
    return value, manifest and log line all said `failed`."""
    _wire_agent_stubs(monkeypatch, tmp_path, persist=None)

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    marker = tmp_path / "notices.txt"
    script = _write_appending_notifier(tmp_path)

    async def operate(self, instruction=None, **kw):
        return ""

    monkeypatch.setattr(Branch, "operate", operate)
    branch_file = tmp_path / "branch.json"
    branch_file.write_text("{}")
    monkeypatch.setattr(agent_mod, "find_branch", lambda rid: (None, branch_file))
    monkeypatch.setattr(Branch, "from_dict", classmethod(lambda cls, data: Branch()))

    result = await agent_mod._run_agent(
        "codex/model",
        "do the thing",
        resume="resumed-branch",
        notify=f"{sys.executable} {script} {marker} {{status}}",
    )

    assert result[3] == "failed", "the conversion this test is about did not happen"
    notices = marker.read_text().split() if marker.exists() else []
    assert notices == ["failed"], f"the notice must agree with the returned status; got {notices}"


@pytest.mark.asyncio
async def test_a_run_that_raises_still_reports_before_the_exception_leaves(monkeypatch, tmp_path):
    """The path where teardown really is the last chance.

    Delivery happens in the tail for every ordinary outcome, because that is
    where the status stops changing. An exception propagating out of the leg
    never reaches the tail, so teardown delivers for it — and a failed run is
    the case the direct path exists for. Without a notice the MCP server that
    wired `--notify` eventually observes a vanished process and publishes an
    indeterminate outcome, which is the opposite of what happened here.
    """
    _wire_agent_stubs(monkeypatch, tmp_path, persist=None)

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    marker = tmp_path / "notices.txt"
    script = _write_appending_notifier(tmp_path)

    async def operate(self, instruction=None, **kw):
        raise RuntimeError("the work itself blew up")

    monkeypatch.setattr(Branch, "operate", operate)

    with pytest.raises(RuntimeError, match="blew up"):
        await agent_mod._run_agent(
            "codex/model",
            "do the thing",
            notify=f"{sys.executable} {script} {marker} {{status}}",
        )

    notices = marker.read_text().split() if marker.exists() else []
    assert notices == ["failed"], (
        f"a run that raised must still report, exactly once; got {notices}"
    )


@pytest.mark.asyncio
async def test_a_failed_pointer_write_does_not_cost_the_run_its_notice(
    monkeypatch, tmp_path, caplog
):
    """The last-branch pointer is written after teardown and before the tail
    that delivers, so anything raising there takes the notice with it.

    The fault injected here is the filesystem's rather than a stand-in raiser:
    the pointer is aimed at a directory and the real helper is put back, because
    what has to survive is a write that fails, not a function someone replaced.
    """
    _wire_agent_stubs(monkeypatch, tmp_path, persist=None)

    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod

    marker = tmp_path / "delivered.txt"
    script = _write_fake_notifier(tmp_path)
    notify_cmd = f"{sys.executable} {script} {marker} {{status}}"

    # The shared harness stubs the pointer write out; this test is about it.
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", runs_mod.save_last_branch_pointer)
    blocked = tmp_path / "pointer-is-a-directory"
    blocked.mkdir()
    monkeypatch.setattr(runs_mod, "_LAST_BRANCH_POINTER", blocked)

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        _res, _provider, _branch_id, status, _session = await agent_mod._run_agent(
            "codex/model", "do the thing", notify=notify_cmd
        )

    assert status == "completed"
    assert marker.exists(), "the pointer write took the terminal notice with it"
    assert marker.read_text() == "completed"
    # Reported, not swallowed: the next `-c` will resume something else, and a
    # silent version of this is worse than the failure it is hiding.
    assert any("last-branch pointer" in rec.message for rec in caplog.records)
