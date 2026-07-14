# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for khive injection configuration in CLI agent profiles."""

import pytest

from lionagi import Branch
from lionagi.agent.factory import register_profile_injection
from lionagi.cli._providers import _parse_profile


def test_profile_parser_promotes_khive_injection_frontmatter():
    profile = _parse_profile(
        "researcher",
        """---
khive_injection:
  profile_id: researcher-recall-v1
  compose:
    enabled: true
---
Research carefully.
""",
    )

    assert profile.khive_injection == {
        "profile_id": "researcher-recall-v1",
        "compose": {"enabled": True},
    }
    assert "khive_injection" not in profile.extra


def test_verbatim_profile_registers_provider_without_calling_khive():
    profile = _parse_profile(
        "researcher",
        """---
khive_injection: true
---
Research carefully.
""",
    )
    branch = Branch(system=profile.system_prompt)

    register_profile_injection(branch, "researcher", profile)

    assert branch.providers.names == ["khive_injection:researcher-recall-v1"]
    provider = branch.providers._entries[0].provider
    assert provider.policy.profile_id == "researcher-recall-v1"


def test_register_profile_injection_keys_on_profile_name():
    # The bare `li agent -a <name>` path passes profile.name as role_name, so the
    # provider is keyed on `{profile.name}-recall-v1` — not the create_agent path's
    # "implementer" default. A reviewer profile derives reviewer-recall-v1.
    profile = _parse_profile("reviewer", "---\nkhive_injection: true\n---\nReview.\n")
    branch = Branch()

    register_profile_injection(branch, profile.name, profile)

    assert branch.providers.names == ["khive_injection:reviewer-recall-v1"]


@pytest.mark.parametrize("configured", [None, False])
def test_register_profile_injection_disabled(configured):
    profile = _parse_profile("reviewer", "---\nkhive_injection: true\n---\nReview.\n")
    profile.khive_injection = configured
    branch = Branch()

    register_profile_injection(branch, "reviewer", profile)

    assert branch.providers.names == []


def test_register_profile_injection_empty_mapping_is_optin():
    # An empty mapping is a valid opt-in that receives the fleet defaults.
    profile = _parse_profile("reviewer", "---\nkhive_injection: true\n---\nReview.\n")
    profile.khive_injection = {}
    branch = Branch()

    register_profile_injection(branch, "reviewer", profile)

    assert branch.providers.names == ["khive_injection:reviewer-recall-v1"]


def test_register_profile_injection_respects_env_killswitch(monkeypatch):
    monkeypatch.setenv("LIONAGI_KHIVE_INJECTION", "0")
    profile = _parse_profile("reviewer", "---\nkhive_injection: true\n---\nReview.\n")
    branch = Branch()

    register_profile_injection(branch, "reviewer", profile)

    assert branch.providers.names == []


@pytest.mark.asyncio
async def test_bare_li_agent_path_registers_injection_from_profile(monkeypatch, tmp_path):
    """End-to-end guard for the bare `li agent -a <profile>` path: a profile that
    opts into khive_injection but has no `role` key (so it takes the plain-Branch
    else-path, not create_agent) must still register the provider — keyed on
    `{profile.name}-recall-v1`. Guards against silently dropping the else-path wiring."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli.agent as agent_mod
    from lionagi.service.manager import iModelManager

    profile = _parse_profile("reviewer", "---\nkhive_injection: true\n---\nReview.\n")
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
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
    monkeypatch.setattr(agent_mod, "setup_agent_persist", AsyncMock(return_value=None))

    async def fake_teardown(ctx, *, status="completed", **kw):
        return status

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

    seen = {}

    async def fake_operate(self, instruction=None, **kw):
        seen["providers"] = list(self.providers.names)
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)

    await agent_mod._run_agent("codex/model", "do the thing", agent_name="reviewer")

    assert seen["providers"] == ["khive_injection:reviewer-recall-v1"]
