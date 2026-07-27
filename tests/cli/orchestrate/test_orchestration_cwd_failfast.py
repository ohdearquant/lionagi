# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o flow` / `li o fanout` (and `li play`, which expands into `li o flow`)
share `setup_orchestration()` as their one setup helper. A nonexistent --cwd
must fail fast there, before any imodel/branch/run is built, instead of
silently reaching a provider spawn.
"""

from __future__ import annotations

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli.orchestrate._orchestration import setup_orchestration


@pytest.mark.asyncio
async def test_setup_orchestration_rejects_nonexistent_cwd_before_any_setup(monkeypatch, tmp_path):
    """A nonexistent --cwd must raise before build_imodel_from_spec/allocate_run
    ever run — i.e. before any provider is spawned or run record created."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    def _boom_build_imodel(*a, **kw):
        raise AssertionError(
            "build_imodel_from_spec must not be reached — cwd validation must fire first"
        )

    def _boom_allocate_run(*a, **kw):
        raise AssertionError("allocate_run must not be reached — cwd validation must fire first")

    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", _boom_build_imodel)
    monkeypatch.setattr(orch_mod, "allocate_run", _boom_allocate_run)

    bad_cwd = str(tmp_path / "nonexistent-workspace")

    with pytest.raises(ConfigurationError) as exc_info:
        await setup_orchestration(
            pattern_name="Fanout",
            model_spec="claude",
            agent_name=None,
            save_dir=None,
            cwd=bad_cwd,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
        )

    msg = str(exc_info.value)
    assert bad_cwd in msg
    assert "--cwd" in msg


@pytest.mark.asyncio
async def test_setup_orchestration_rejects_cwd_that_is_a_file(monkeypatch, tmp_path):
    f = tmp_path / "im-a-file.txt"
    f.write_text("x")

    with pytest.raises(ConfigurationError) as exc_info:
        await setup_orchestration(
            pattern_name="Flow",
            model_spec="claude",
            agent_name=None,
            save_dir=None,
            cwd=str(f),
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
        )
    assert "not a directory" in str(exc_info.value)


@pytest.mark.asyncio
async def test_setup_orchestration_none_cwd_is_unaffected(monkeypatch):
    """cwd=None (no --cwd given) must not be rejected by the cwd check — it
    must carry on into the rest of setup.

    This used to be shown by falling through to a 'model spec required' error.
    A call naming neither an agent nor a model now resolves to the orchestrator
    profile rather than refusing, so that error is no longer reachable here and
    cannot serve as the marker. Stop the call at the first thing built instead,
    which proves the same point: cwd validation let a cwd=None call through.

    The call names a model so that nothing here depends on which agent profiles
    happen to exist on the machine running the test.
    """
    import lionagi.cli.orchestrate._orchestration as orch_mod

    class _ReachedBuild(Exception):
        pass

    def _stop(*a, **kw):
        raise _ReachedBuild

    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", _stop)

    with pytest.raises(_ReachedBuild):
        await setup_orchestration(
            pattern_name="Flow",
            model_spec="claude",
            agent_name=None,
            save_dir=None,
            cwd=None,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
        )
