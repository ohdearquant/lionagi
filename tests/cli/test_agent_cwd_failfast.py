# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent --cwd <nonexistent>` must fail fast, before any run is allocated
or any agent is spawned, instead of the provider layer silently creating the
directory (or a deep, opaque subprocess failure reported as a clean success).

Covers the shared validator (`lionagi.cli._util.validate_cwd_exists`) and its
wiring into both the async engine (`_run_agent`) and the sync CLI entry point
(`run_agent`).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli._util import validate_cwd_exists

# ---------------------------------------------------------------------------
# validate_cwd_exists — the shared validator
# ---------------------------------------------------------------------------


class TestValidateCwdExists:
    def test_none_is_a_noop(self):
        assert validate_cwd_exists(None) is None  # must not raise

    def test_empty_string_is_a_noop(self):
        assert validate_cwd_exists("") == ""  # must not raise

    def test_existing_directory_passes(self, tmp_path):
        assert validate_cwd_exists(str(tmp_path)) == str(tmp_path)  # must not raise

    def test_tilde_path_to_existing_dir_returns_expanded(self, tmp_path, monkeypatch):
        """A `--cwd=~/...` value must come back tilde-expanded: validating the
        expanded path while forwarding the literal would pass validation and
        then fail deep in the provider layer, which never expands `~`."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "proj").mkdir()
        assert validate_cwd_exists("~/proj") == str(tmp_path / "proj")

    def test_tilde_path_to_nonexistent_dir_still_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists("~/does-not-exist-xyz")
        assert "~/does-not-exist-xyz" in str(exc_info.value)

    def test_nonexistent_path_raises_naming_path_and_flag(self, tmp_path):
        bad = str(tmp_path / "does-not-exist-xyz")
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists(bad)
        msg = str(exc_info.value)
        assert bad in msg
        assert "--cwd" in msg

    def test_path_that_is_a_file_raises_not_a_directory(self, tmp_path):
        f = tmp_path / "im-a-file.txt"
        f.write_text("x")
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists(str(f))
        msg = str(exc_info.value)
        assert str(f) in msg
        assert "not a directory" in msg

    def test_custom_flag_name_is_reflected_in_message(self, tmp_path):
        bad = str(tmp_path / "nope")
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists(bad, flag="--workspace")
        assert "--workspace" in str(exc_info.value)
        assert "--cwd" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_agent: fails before any spawn / run allocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_rejects_nonexistent_cwd_before_any_spawn(monkeypatch, tmp_path):
    """A nonexistent --cwd must raise before allocate_run/build_chat_model/
    branch.operate ever run — i.e. before any run record could be created and
    before any provider CLI could be spawned."""
    import lionagi.cli.agent as agent_mod

    def _boom_allocate_run():
        raise AssertionError("allocate_run must not be reached — cwd validation must fire first")

    def _boom_build_chat_model(*a, **kw):
        raise AssertionError(
            "build_chat_model must not be reached — cwd validation must fire first"
        )

    monkeypatch.setattr(agent_mod, "allocate_run", _boom_allocate_run)
    monkeypatch.setattr(agent_mod, "build_chat_model", _boom_build_chat_model)

    bad_cwd = str(tmp_path / "nonexistent-workspace")

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError) as exc_info:
        await _run_agent("claude", "do the thing", cwd=bad_cwd)

    msg = str(exc_info.value)
    assert bad_cwd in msg
    assert "--cwd" in msg


def _patch_agent_happy_path(monkeypatch, tmp_path) -> dict:
    """Stub out everything around _run_agent's spawn so a test can observe
    exactly what `repo` (the forwarded cwd) reaches Branch.operate."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

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
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )

    spawned = {}

    async def fake_operate(self, instruction=None, **kw):
        spawned["called"] = True
        spawned["repo"] = kw.get("repo")
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    return spawned


@pytest.mark.asyncio
async def test_run_agent_accepts_existing_cwd_and_reaches_spawn(monkeypatch, tmp_path):
    """Regression guard: a *valid* --cwd must not be rejected — validation
    only fires for a genuinely missing/non-directory path."""
    import lionagi.cli.agent as agent_mod

    spawned = _patch_agent_happy_path(monkeypatch, tmp_path)

    _result, _provider, _bid, terminal_status, _sid = await agent_mod._run_agent(
        "codex/model", "do the thing", cwd=str(tmp_path)
    )

    assert terminal_status == "completed"
    assert spawned.get("called") is True
    assert spawned.get("repo") == str(tmp_path)


@pytest.mark.asyncio
async def test_run_agent_forwards_tilde_expanded_cwd_to_spawn(monkeypatch, tmp_path):
    """A `--cwd=~/...` value must reach the spawn tilde-expanded — the
    provider layer never expands `~`, so forwarding the literal would fail
    deep in the subprocess despite passing validation."""
    import lionagi.cli.agent as agent_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "ws").mkdir()
    spawned = _patch_agent_happy_path(monkeypatch, tmp_path)

    _result, _provider, _bid, terminal_status, _sid = await agent_mod._run_agent(
        "codex/model", "do the thing", cwd="~/ws"
    )

    assert terminal_status == "completed"
    assert spawned.get("repo") == str(tmp_path / "ws")


# ---------------------------------------------------------------------------
# run_agent (sync CLI entry point): clean diagnostic before re-raising
# ---------------------------------------------------------------------------


def _agent_args(**overrides) -> SimpleNamespace:
    base = dict(
        query=["claude", "do the thing"],
        prompt_flag=None,
        prompt_file=None,
        yolo=False,
        verbose=False,
        theme=None,
        resume=None,
        continue_last=False,
        effort=None,
        agent=None,
        cwd=None,
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        preset=None,
        form=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_run_agent_cli_nonzero_and_diagnostic_for_nonexistent_cwd(monkeypatch, tmp_path):
    """The full sync CLI entry point (`run_agent`) must surface a clear,
    one-line diagnostic naming the path and --cwd before the exception
    propagates (which the console-script wrapper turns into a nonzero
    process exit — see classify_exception -> EXIT_CODE_BY_STATUS['failed'])."""
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._util import EXIT_CODE_BY_STATUS, classify_exception

    errors_emitted: list[str] = []
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors_emitted.append(msg))

    bad_cwd = str(tmp_path / "nonexistent-workspace")

    from lionagi.cli.agent import run_agent

    with pytest.raises(ConfigurationError) as exc_info:
        run_agent(_agent_args(cwd=bad_cwd))

    # classify_exception must route this to the "failed" (nonzero exit) bucket.
    assert classify_exception(exc_info.value) == "failed"
    assert EXIT_CODE_BY_STATUS["failed"] != 0

    assert errors_emitted, "log_error must be called with a clear diagnostic"
    combined = " ".join(errors_emitted)
    assert bad_cwd in combined
    assert "--cwd" in combined


def test_run_agent_cli_no_run_allocated_for_nonexistent_cwd(monkeypatch, tmp_path):
    """No run record may be created at all when --cwd fails validation —
    stronger than merely 'not completed.ok'."""
    import lionagi.cli.agent as agent_mod

    def _boom_allocate_run():
        raise AssertionError("allocate_run must not be reached")

    monkeypatch.setattr(agent_mod, "allocate_run", _boom_allocate_run)
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: None)

    bad_cwd = str(tmp_path / "nonexistent-workspace")

    from lionagi.cli.agent import run_agent

    with pytest.raises(ConfigurationError):
        run_agent(_agent_args(cwd=bad_cwd))
