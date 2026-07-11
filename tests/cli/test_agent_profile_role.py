# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the opt-in profile frontmatter key `role`.

Covers:
  - `role: reviewer` in a profile -> `-a reviewer` alone (no --preset coding)
    takes the create_agent path; system message carries the reviewer policy
    block, not the implementer's.
  - `role: <unknown>` -> Role.load's ValueError surfaces, not swallowed into
    a bare Branch fallback.
  - A profile with no `role:` key keeps the plain Branch(...) path exactly
    (regression across every existing shipped profile shape).
  - `--preset coding` with no `-a` (no profile at all) is unaffected — the
    default role still resolves to "implementer".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli._providers import _parse_profile

# ---------------------------------------------------------------------------
# Shared stub wiring (mirrors tests/cli/test_agent_profile_timeout.py)
# ---------------------------------------------------------------------------


def _wire_agent_stubs(monkeypatch, tmp_path: Path):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    branches_created: list = []
    real_branch_init = Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(Branch, "__init__", spy_branch_init)

    async def fake_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")

    async def fake_setup(*a, **kw):
        return {"session_id": "sess-0"}

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
    return branches_created


def _stub_profile(monkeypatch, name: str, text: str):
    """Wire load_agent_profile to return a real AgentProfile parsed from `text`."""
    import lionagi.cli.agent as agent_mod

    profile = _parse_profile(name, text)
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda n: profile)
    return profile


# ---------------------------------------------------------------------------
# Item 1: role: reviewer -> create_agent path, reviewer policy block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_role_key_takes_create_agent_path_with_reviewer_policy(monkeypatch, tmp_path):
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "reviewer",
        "---\nmodel: claude_code/sonnet\nrole: reviewer\n---\nExtra reviewer body.",
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "review this", agent_name="reviewer")

    assert branches_created
    branch = branches_created[-1]
    # create_agent wired CodingToolkit tools onto the branch (bare Branch()
    # never registers any tools) — the clearest signal the create_agent path
    # ran rather than the plain Branch(...) path.
    assert "bash" in branch.acts.registry

    rendered = branch.msgs.system.rendered
    assert "## Authority" in rendered
    # Reviewer's own boundary text (default.yaml), not the implementer's.
    assert "cannot override a critic" in rendered.lower() or "approve" in rendered.lower()
    assert "improve structure" not in rendered.lower(), (
        "reviewer leg must not carry the implementer's policy block"
    )


@pytest.mark.asyncio
async def test_profile_role_key_without_preset_flag(monkeypatch, tmp_path):
    """The role key alone (no --preset coding) is sufficient to switch paths."""
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch, "reviewer", "---\nmodel: claude_code/sonnet\nrole: reviewer\n---\nbody"
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "go", agent_name="reviewer", preset=None)

    branch = branches_created[-1]
    assert "bash" in branch.acts.registry  # create_agent path, not bare Branch


# ---------------------------------------------------------------------------
# Falsy explicit `role:` values must fail closed, not silently default to
# "implementer" (a role profile with role: "" / false / 0 must not silently
# be granted implementer coding authority).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "yaml_role_literal",
    ['""', "false", "0"],
    ids=["empty-string", "false", "zero"],
)
async def test_profile_falsy_role_value_raises_configuration_error(
    monkeypatch, tmp_path, yaml_role_literal
):
    """`role: ""`, `role: false`, `role: 0` must raise, never silently
    resolve to the implementer default via `profile_role or "implementer"`.
    """
    from lionagi._errors import ConfigurationError

    _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "falsy-role-profile",
        f"---\nmodel: claude_code/sonnet\nrole: {yaml_role_literal}\n---\nbody",
    )

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ConfigurationError):
        await _run_agent(None, "go", agent_name="falsy-role-profile")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "yaml_role_literal",
    ['""', "false", "0"],
    ids=["empty-string", "false", "zero"],
)
async def test_profile_falsy_role_value_raises_on_resume_too(
    monkeypatch, tmp_path, yaml_role_literal
):
    """The falsy-role validation runs before the resume/new-branch split: a
    malformed profile fails loudly on `--resume` / `--continue-last`
    invocations as well, not only when a new branch is composed.
    """
    from lionagi._errors import ConfigurationError

    _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "falsy-role-profile",
        f"---\nmodel: claude_code/sonnet\nrole: {yaml_role_literal}\n---\nbody",
    )

    import lionagi.cli.agent as agent_mod
    from lionagi.cli.agent import _run_agent

    # find_branch must not be reached — validation fires first. Make it blow
    # up loudly if the code ever gets that far, so this test cannot pass by
    # accidentally resuming something.
    def _boom(_id):
        raise AssertionError("resume lookup reached before role validation")

    monkeypatch.setattr(agent_mod, "find_branch", _boom)

    with pytest.raises(ConfigurationError):
        await _run_agent(None, "go", agent_name="falsy-role-profile", resume="deadbeef")


# ---------------------------------------------------------------------------
# Item 2: unknown role -> ValueError surfaces, not swallowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_role_unknown_raises_value_error(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "ghost-role-profile",
        "---\nmodel: claude_code/sonnet\nrole: nonexistent-role-xyz\n---\nbody",
    )

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ValueError, match="Unknown role"):
        await _run_agent(None, "go", agent_name="ghost-role-profile")


# ---------------------------------------------------------------------------
# Item 4: profile without role: key -> byte-for-byte unchanged (bare Branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_without_role_key_keeps_bare_branch_path(monkeypatch, tmp_path):
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(monkeypatch, "researcher", "---\nmodel: claude_code/sonnet\n---\nDo research.")

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "go", agent_name="researcher")

    branch = branches_created[-1]
    # No coding tools registered — the plain Branch(...) path never wires them.
    assert not {"bash", "reader", "editor", "search"}.intersection(branch.acts.registry.keys())
    assert branch.msgs.system is not None
    assert "Do research." in branch.msgs.system.rendered


@pytest.mark.asyncio
async def test_profile_with_unrelated_frontmatter_keys_no_role_key(monkeypatch, tmp_path):
    """A profile using other 'extra' frontmatter keys but no 'role' key must
    still take the bare-Branch path (role is opt-in, never inferred)."""
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "advisor",
        "---\nmodel: claude_code/sonnet\nsome_custom_key: whatever\n---\nAdvisor body.",
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "go", agent_name="advisor")

    branch = branches_created[-1]
    assert not {"bash", "reader", "editor", "search"}.intersection(branch.acts.registry.keys())


# ---------------------------------------------------------------------------
# Item 7: --preset coding with no -a is unaffected (default role implementer)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# lion_system: false must propagate to the create_agent path (role: key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_profile_lion_system_false_suppresses_lion_preamble(monkeypatch, tmp_path):
    """A role profile with `lion_system: false` must not carry the LION system
    preamble even though it takes the create_agent path (AgentSpec.coding()
    defaults lion_system=True regardless of the profile's own frontmatter)."""
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "reviewer-nolion",
        "---\nmodel: claude_code/sonnet\nrole: reviewer\nlion_system: false\n---\nBare reviewer body.",
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "review this", agent_name="reviewer-nolion")

    branch = branches_created[-1]
    assert "bash" in branch.acts.registry  # create_agent path ran

    rendered = branch.msgs.system.rendered
    assert "# Welcome to LIONAGI" not in rendered
    assert "Bare reviewer body." in rendered
    assert "## Authority" in rendered  # role policy block still composed


@pytest.mark.asyncio
async def test_role_profile_lion_system_default_true_keeps_preamble(monkeypatch, tmp_path):
    """Sanity counterpart: a role profile with no lion_system key (default
    True) must still carry the LION preamble on the create_agent path."""
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)
    _stub_profile(
        monkeypatch,
        "reviewer-lion",
        "---\nmodel: claude_code/sonnet\nrole: reviewer\n---\nBare reviewer body.",
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent(None, "review this", agent_name="reviewer-lion")

    branch = branches_created[-1]
    rendered = branch.msgs.system.rendered
    assert "# Welcome to LIONAGI" in rendered


@pytest.mark.asyncio
async def test_preset_coding_without_agent_name_unaffected(monkeypatch, tmp_path):
    branches_created = _wire_agent_stubs(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude_code/sonnet", "go", preset="coding")

    branch = branches_created[-1]
    assert "bash" in branch.acts.registry
    assert "implementer" in branch.msgs.system.rendered.lower()
