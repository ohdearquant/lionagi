# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o flow` / `li o fanout` (and `li play`, which expands into `li o flow`)
share `setup_orchestration()`. Naming neither an agent nor a model there is a
request to orchestrate, not an incomplete command, so it resolves to the
orchestrator profile instead of refusing.

The refusal survives for the case it was written for: a caller who did name an
agent, whose profile carries no model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli.orchestrate._orchestration import (
    DEFAULT_ORCHESTRATOR_AGENT,
    setup_orchestration,
)


class _Reached(Exception):
    """Raised in place of building an imodel, to stop the call once the
    agent/model resolution under test has already happened."""


@pytest.fixture
def resolution_probe(monkeypatch, tmp_path):
    """Capture which profile name setup_orchestration loads, then stop the call
    before it builds anything. Returns the list of names passed to
    load_agent_profile."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    loaded: list[str] = []

    def _fake_load_agent_profile(name, *a, **kw):
        loaded.append(name)
        return SimpleNamespace(model="claude", effort=None, yolo=False, fast_mode=False)

    def _stop(*a, **kw):
        raise _Reached

    monkeypatch.setattr(orch_mod, "load_agent_profile", _fake_load_agent_profile)
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", _stop)
    return loaded


async def _run(**overrides):
    kwargs = dict(
        pattern_name="Fanout",
        model_spec=None,
        agent_name=None,
        save_dir=None,
        cwd=None,
        yolo=False,
        verbose=False,
        effort=None,
        theme=None,
    )
    kwargs.update(overrides)
    return await setup_orchestration(**kwargs)


@pytest.mark.asyncio
async def test_naming_neither_agent_nor_model_defaults_to_the_orchestrator(resolution_probe):
    """The bare case is the one the directive is about: no agent, no model."""
    with pytest.raises(_Reached):
        await _run()

    assert resolution_probe == [DEFAULT_ORCHESTRATOR_AGENT]


@pytest.mark.asyncio
async def test_a_named_model_is_honoured_and_loads_no_profile(resolution_probe):
    """Naming compute is still naming compute — the default must not override
    it, and must not drag a profile in behind it."""
    with pytest.raises(_Reached):
        await _run(model_spec="claude")

    assert resolution_probe == []


@pytest.mark.asyncio
async def test_a_named_agent_is_honoured_over_the_default(resolution_probe):
    with pytest.raises(_Reached):
        await _run(agent_name="reviewer")

    assert resolution_probe == ["reviewer"]


@pytest.mark.asyncio
async def test_a_named_agent_with_no_model_still_refuses_and_names_itself(monkeypatch):
    """The refusal is not deleted, it is narrowed. It now fires only where the
    caller chose an agent we could not resolve to a model, so it says which."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    def _modelless_profile(name, *a, **kw):
        return SimpleNamespace(model=None, effort=None, yolo=False, fast_mode=False)

    def _boom(*a, **kw):
        raise AssertionError("must refuse before building an imodel")

    monkeypatch.setattr(orch_mod, "load_agent_profile", _modelless_profile)
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", _boom)

    with pytest.raises(ConfigurationError) as exc_info:
        await _run(agent_name="profile-without-a-model")

    assert "profile-without-a-model" in str(exc_info.value)


@pytest.mark.asyncio
async def test_the_default_does_not_fire_when_a_modelless_agent_was_named(monkeypatch):
    """Guards the interaction between the two behaviours: a modelless named
    agent must reach the refusal, never silently fall through to the default
    and orchestrate under compute the caller did not ask for."""
    import lionagi.cli.orchestrate._orchestration as orch_mod

    loaded: list[str] = []

    def _modelless_profile(name, *a, **kw):
        loaded.append(name)
        return SimpleNamespace(model=None, effort=None, yolo=False, fast_mode=False)

    monkeypatch.setattr(orch_mod, "load_agent_profile", _modelless_profile)

    with pytest.raises(ConfigurationError):
        await _run(agent_name="profile-without-a-model")

    assert loaded == ["profile-without-a-model"]
    assert DEFAULT_ORCHESTRATOR_AGENT not in loaded
