# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Role-profile routing cache correctness, run-derived khive-injection
namespacing, and effort-suffix precedence through EngineRun.make_agent().
No LLM."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import lionagi.cli._providers as providers_mod
import lionagi.engines.engine as engine_mod
from lionagi.engines.engine import Engine
from lionagi.tools.khive_injection import KhiveInjectionProvider

# ---------------------------------------------------------------------------
# Role-profile cache: keyed by (role, cwd), never caches an exception fallback
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    monkeypatch.setattr(engine_mod, "_ROLE_PROFILE_CACHE", {})
    monkeypatch.setattr(engine_mod, "_ROLE_INJECTION_CACHE", {})


def test_role_profile_cache_keyed_by_resolved_project_dir(monkeypatch, tmp_path):
    calls: list[tuple[str, str]] = []

    def fake_load(role):
        calls.append((role, os.getcwd()))
        return SimpleNamespace(model="m-from-profile", effort="high")

    monkeypatch.setattr(providers_mod, "load_agent_profile", fake_load)

    dir_a = tmp_path / "project-a"
    dir_b = tmp_path / "project-b"
    dir_a.mkdir()
    dir_b.mkdir()

    monkeypatch.chdir(dir_a)
    first = engine_mod.role_profile_route("implementer")
    first_again = engine_mod.role_profile_route("implementer")  # cache hit, same cwd

    monkeypatch.chdir(dir_b)
    second = engine_mod.role_profile_route("implementer")  # different cwd, must re-resolve

    assert first == first_again == ("m-from-profile", "high")
    assert second == ("m-from-profile", "high")
    # One real lookup per distinct (role, cwd) pair, not one for the whole process.
    assert len(calls) == 2


def test_role_profile_cache_does_not_cache_missing_profile(monkeypatch):
    calls = {"n": 0}

    def fake_load(role):
        calls["n"] += 1
        raise FileNotFoundError("no such profile")

    monkeypatch.setattr(providers_mod, "load_agent_profile", fake_load)

    assert engine_mod.role_profile_route("ghost") == (None, None)
    assert engine_mod.role_profile_route("ghost") == (None, None)
    # A missing profile must not be cached — a profile created later (or by a
    # cwd change back to a project that has one) must be picked up.
    assert calls["n"] == 2


def test_role_profile_cache_does_not_cache_parse_failure(monkeypatch):
    calls = {"n": 0}

    def fake_load(role):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("malformed frontmatter")
        return SimpleNamespace(model="fixed/model", effort="medium")

    monkeypatch.setattr(providers_mod, "load_agent_profile", fake_load)

    first = engine_mod.role_profile_route("flaky")
    assert first == (None, None)
    second = engine_mod.role_profile_route("flaky")
    assert second == ("fixed/model", "medium")
    assert calls["n"] == 2  # retried on the next call, not served a cached failure


def test_role_profile_injection_cache_keyed_by_cwd_and_not_negative(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_load(role):
        calls.append(os.getcwd())
        return SimpleNamespace(khive_injection=True)

    monkeypatch.setattr(providers_mod, "load_agent_profile", fake_load)

    dir_a = tmp_path / "proj-a"
    dir_b = tmp_path / "proj-b"
    dir_a.mkdir()
    dir_b.mkdir()

    monkeypatch.chdir(dir_a)
    assert engine_mod.role_profile_injection("researcher") is True
    assert engine_mod.role_profile_injection("researcher") is True
    monkeypatch.chdir(dir_b)
    assert engine_mod.role_profile_injection("researcher") is True

    assert len(calls) == 2  # one lookup per distinct project dir


# ---------------------------------------------------------------------------
# make_agent(): run-derived khive-injection namespace
# ---------------------------------------------------------------------------


def _provider_entries(branch):
    return list(branch.providers._entries)


@pytest.mark.asyncio
async def test_make_agent_stamps_run_derived_namespace_bool_opt_in():
    eng = Engine(khive_injection=True)
    run = eng.new_run()
    branch = await run.make_agent("researcher", name="r1")

    entries = _provider_entries(branch)
    khive_entries = [e for e in entries if isinstance(e.provider, KhiveInjectionProvider)]
    assert len(khive_entries) == 1
    namespace = khive_entries[0].provider.policy.namespace
    assert namespace == f"engine:{run.run_id}"


@pytest.mark.asyncio
async def test_make_agent_preserves_caller_pinned_namespace():
    eng = Engine(khive_injection={"namespace": "caller-pinned-ns"})
    run = eng.new_run()
    branch = await run.make_agent("researcher", name="r1")

    entries = _provider_entries(branch)
    khive_entries = [e for e in entries if isinstance(e.provider, KhiveInjectionProvider)]
    assert khive_entries[0].provider.policy.namespace == "caller-pinned-ns"


@pytest.mark.asyncio
async def test_make_agent_two_runs_get_distinct_namespaces():
    eng = Engine(khive_injection=True)
    run1 = eng.new_run()
    run2 = eng.new_run()
    b1 = await run1.make_agent("researcher", name="r1")
    b2 = await run2.make_agent("researcher", name="r2")

    ns1 = _provider_entries(b1)[0].provider.policy.namespace
    ns2 = _provider_entries(b2)[0].provider.policy.namespace
    assert ns1 != ns2  # cross-run memory exposure is exactly what this closes


# ---------------------------------------------------------------------------
# make_agent(): effort-suffix precedence over a profile default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_agent_model_effort_suffix_survives_profile_default(monkeypatch):
    """A model spec's baked-in effort suffix must not be silently overridden by
    the role's agent-profile effort default."""
    monkeypatch.setattr(
        engine_mod, "role_profile_route", lambda role: (None, "low" if role == "critic" else None)
    )
    eng = Engine()
    run = eng.new_run()
    branch = await run.make_agent("critic", name="c1", model="codex/gpt-5.6-luna-high")

    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("reasoning_effort") == "high"  # suffix wins, not the profile's "low"


@pytest.mark.asyncio
async def test_make_agent_explicit_effort_still_beats_profile():
    eng = Engine()
    run = eng.new_run()
    branch = await run.make_agent("critic", name="c1", model="codex/gpt-5.6-luna", effort="xhigh")
    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("reasoning_effort") == "xhigh"


@pytest.mark.asyncio
async def test_make_agent_profile_effort_applies_when_model_has_no_suffix(monkeypatch):
    monkeypatch.setattr(
        engine_mod, "role_profile_route", lambda role: (None, "low" if role == "critic" else None)
    )
    eng = Engine()
    run = eng.new_run()
    branch = await run.make_agent("critic", name="c1", model="codex/gpt-5.6-luna")

    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs.get("reasoning_effort") == "low"
