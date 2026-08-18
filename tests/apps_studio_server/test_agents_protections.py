# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Studio agent create/delete and the system/default agent protections.

lionagi/studio/services/agents.py previously left POST and DELETE unimplemented
(501). These tests exercise the create/delete/update paths added to support
multiple, independently-configured agent versions per cast role, and verify the
protection rules run in both directions: a protected agent refuses the write,
and an ordinary agent accepts it (a test that would pass even if every write
were blocked proves nothing)."""

from __future__ import annotations

import textwrap

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("yaml", reason="PyYAML not installed")


def _write_agent_md(path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


def _make_agents_root(tmp_path, monkeypatch):
    import lionagi.studio.services.agents as agents_mod

    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", root)
    return root


# create_agent()


def test_create_agent_writes_file_and_defaults_to_editable(tmp_path, monkeypatch):
    """A freshly created agent is never system-owned, regardless of what's posted."""
    from lionagi.studio.services.agents import create_agent, get_agent

    _make_agents_root(tmp_path, monkeypatch)

    created = create_agent(
        "my-critic",
        {
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "role": "critic",
            "system_prompt": "Be harsh.",
            "lion_system": True,  # client-supplied; must be ignored
        },
    )

    assert created["name"] == "my-critic"
    assert created["lion_system"] is False
    assert created["role"] == "critic"
    assert created["system_prompt"] == "Be harsh."

    fresh = get_agent("my-critic")
    assert fresh is not None
    assert fresh["lion_system"] is False


def test_create_agent_rejects_duplicate_name(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import AgentExistsError, create_agent

    _make_agents_root(tmp_path, monkeypatch)

    create_agent("dup", {"provider": "claude", "model": "claude-sonnet-4-6"})
    with pytest.raises(AgentExistsError):
        create_agent("dup", {"provider": "claude", "model": "claude-sonnet-4-6"})


def test_create_agent_rejects_unknown_role(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import create_agent

    _make_agents_root(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unknown cast role"):
        create_agent("bogus-role-agent", {"role": "not-a-real-role"})


def test_create_agent_rejects_unknown_mode(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import create_agent

    _make_agents_root(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unknown cast mode"):
        create_agent("bogus-mode-agent", {"mode": "not-a-real-mode"})


def test_create_two_versions_of_the_same_role(tmp_path, monkeypatch):
    """Two independently named, independently configured agents can both wrap 'critic'."""
    from lionagi.casts.pattern import list_modes
    from lionagi.studio.services.agents import create_agent, list_agents

    _make_agents_root(tmp_path, monkeypatch)
    modes = list_modes()
    assert len(modes) >= 2, "need at least two real modes to prove they aren't invented"

    create_agent(
        "critic-fast",
        {"role": "critic", "model": "claude-haiku-4-5", "effort": "low", "mode": modes[0]},
    )
    create_agent(
        "critic-deep",
        {"role": "critic", "model": "claude-opus-5", "effort": "xhigh", "mode": modes[1]},
    )

    agents = {a["name"]: a for a in list_agents()}
    assert agents["critic-fast"]["role"] == "critic"
    assert agents["critic-deep"]["role"] == "critic"
    assert agents["critic-fast"]["mode"] == modes[0]
    assert agents["critic-deep"]["mode"] == modes[1]
    assert agents["critic-fast"]["model"] != agents["critic-deep"]["model"]


def test_every_cast_role_can_become_an_agent_template(tmp_path, monkeypatch):
    """Every built-in role name is accepted by create_agent's role validation."""
    from lionagi.casts.pattern import list_roles
    from lionagi.studio.services.agents import _canonical_role

    _make_agents_root(tmp_path, monkeypatch)
    roles = list_roles()
    assert len(roles) > 5, "sanity: the built-in role catalog should not be near-empty"
    for role in roles:
        assert _canonical_role(role) == role  # must not raise, must not alter


def test_create_agent_stores_the_role_it_validated(tmp_path, monkeypatch):
    """Padded input must be stored stripped: the runtime's role lookup is exact, so
    persisting ' critic ' after validating 'critic' writes a profile that passes the
    API and then fails to launch."""
    from lionagi.casts.pattern import list_roles
    from lionagi.studio.services.agents import create_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    roles = list_roles()
    assert "critic" in roles, "sanity: this test's padded value must wrap a real role"

    created = create_agent("padded-role", {"role": "  critic  "})

    assert created["role"] == "critic"
    assert created["role"] in roles
    assert "role: critic\n" in (root / "padded-role.md").read_text()


def test_create_agent_stores_the_mode_it_validated(tmp_path, monkeypatch):
    from lionagi.casts.pattern import list_modes
    from lionagi.studio.services.agents import create_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    modes = list_modes()
    assert modes, "sanity: the built-in mode catalog should not be empty"
    mode = modes[0]

    created = create_agent("padded-mode", {"mode": f"  {mode}  "})

    assert created["mode"] == mode
    assert created["mode"] in modes
    assert f"mode: {mode}\n" in (root / "padded-mode.md").read_text()


def test_update_agent_stores_the_role_and_mode_it_validated(tmp_path, monkeypatch):
    """The PUT path canonicalises for the same reason the create path does."""
    from lionagi.casts.pattern import list_modes, list_roles
    from lionagi.studio.services.agents import create_agent, get_agent, update_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    roles = list_roles()
    modes = list_modes()
    assert "critic" in roles and modes
    mode = modes[0]

    create_agent("edited", {"provider": "claude", "model": "claude-sonnet-4-6"})
    updated = update_agent("edited", {"role": "\tcritic ", "mode": f" {mode}\n"})

    assert updated is not None
    assert updated["role"] == "critic"
    assert updated["mode"] == mode

    text = (root / "edited.md").read_text()
    assert "role: critic\n" in text
    assert f"mode: {mode}\n" in text

    fresh = get_agent("edited")
    assert fresh["role"] in roles
    assert fresh["mode"] in modes


def test_padded_unknown_role_is_still_rejected(tmp_path, monkeypatch):
    """Canonicalising must not become a way to smuggle an unknown role past the check."""
    from lionagi.studio.services.agents import create_agent

    _make_agents_root(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unknown cast role"):
        create_agent("padded-bogus-role", {"role": "  not-a-real-role  "})


# delete_agent() protections


def test_delete_ordinary_agent_succeeds(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import create_agent, delete_agent, get_agent

    _make_agents_root(tmp_path, monkeypatch)
    create_agent("throwaway", {"provider": "claude", "model": "claude-sonnet-4-6"})

    assert delete_agent("throwaway") is True
    assert get_agent("throwaway") is None


def test_delete_missing_agent_returns_false(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import delete_agent

    _make_agents_root(tmp_path, monkeypatch)
    assert delete_agent("never-existed") is False


def test_delete_agent_without_lion_system_key_is_not_protected(tmp_path, monkeypatch):
    """A plain file with no lion_system key at all (common for hand-authored profiles
    and the generic definitions save path) is NOT treated as a system agent -- only an
    explicit lion_system: true is. get_agent() still displays lion_system: true for such
    a file (CLI-parity default), but that display default must not gate deletion."""
    from lionagi.studio.services.agents import delete_agent, get_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    _write_agent_md(
        root / "hand-authored.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        role: critic
        ---
        No lion_system key at all.
        """,
    )

    assert get_agent("hand-authored")["lion_system"] is True  # display default
    assert get_agent("hand-authored")["protected"] is False  # but not write-protected
    assert delete_agent("hand-authored") is True


def test_delete_explicit_system_agent_refused(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import AgentProtectedError, delete_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    _write_agent_md(
        root / "sysagent.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: true
        ---
        System prompt.
        """,
    )

    with pytest.raises(AgentProtectedError):
        delete_agent("sysagent")


def test_delete_quoted_truthy_lion_system_is_protected(tmp_path, monkeypatch):
    """YAML permits ``lion_system: "true"`` (a quoted string) as well as the bare
    boolean. The runtime (lionagi/cli/_providers.py) treats both as system-owned via
    bool(...), so the delete guard must refuse both too, not just the unquoted form."""
    from lionagi.studio.services.agents import AgentProtectedError, delete_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    _write_agent_md(
        root / "quotedsys.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: "true"
        ---
        System prompt.
        """,
    )

    with pytest.raises(AgentProtectedError):
        delete_agent("quotedsys")
    assert (root / "quotedsys.md").exists()


def test_delete_default_agent_refused_even_when_user_owned(tmp_path, monkeypatch):
    """The 'default' agent cannot be deleted even though it is otherwise a normal,
    editable (lion_system: false) agent -- name-based protection, not system-based."""
    from lionagi.studio.services.agents import (
        DEFAULT_AGENT_NAME,
        AgentProtectedError,
        create_agent,
        delete_agent,
        get_agent,
    )

    root = _make_agents_root(tmp_path, monkeypatch)
    assert DEFAULT_AGENT_NAME == "default"
    create_agent(DEFAULT_AGENT_NAME, {"provider": "claude", "model": "claude-sonnet-4-6"})
    assert get_agent(DEFAULT_AGENT_NAME)["lion_system"] is False  # editable, not system

    with pytest.raises(AgentProtectedError):
        delete_agent(DEFAULT_AGENT_NAME)
    assert (root / "default.md").exists()


# update_agent() protections


def test_edit_system_agent_refused(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import AgentProtectedError, update_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    _write_agent_md(
        root / "sysagent.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: true
        ---
        Original prompt.
        """,
    )

    with pytest.raises(AgentProtectedError):
        update_agent("sysagent", {"model": "claude-opus-5"})

    # Untouched on disk.
    assert "Original prompt." in (root / "sysagent.md").read_text()
    assert "claude-opus-5" not in (root / "sysagent.md").read_text()


def test_edit_quoted_truthy_lion_system_is_protected(tmp_path, monkeypatch):
    """Same quoted-string case as the delete guard, for the update path."""
    from lionagi.studio.services.agents import AgentProtectedError, update_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    _write_agent_md(
        root / "quotedsys.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: "true"
        ---
        Original prompt.
        """,
    )

    with pytest.raises(AgentProtectedError):
        update_agent("quotedsys", {"model": "claude-opus-5"})

    assert "Original prompt." in (root / "quotedsys.md").read_text()
    assert "claude-opus-5" not in (root / "quotedsys.md").read_text()


def test_edit_agent_without_lion_system_key_is_not_protected(tmp_path, monkeypatch):
    """Pins the deliberate exception on the update path: a profile with no
    lion_system key at all is still editable (see the delete-path equivalent,
    test_delete_agent_without_lion_system_key_is_not_protected, for the rationale)."""
    from lionagi.studio.services.agents import update_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    _write_agent_md(
        root / "hand-authored.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        ---
        No lion_system key at all.
        """,
    )

    updated = update_agent("hand-authored", {"effort": "high"})
    assert updated is not None
    assert updated["effort"] == "high"


def test_edit_ordinary_agent_succeeds(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import create_agent, update_agent

    _make_agents_root(tmp_path, monkeypatch)
    create_agent("editable", {"provider": "claude", "model": "claude-sonnet-4-6"})

    updated = update_agent("editable", {"effort": "high"})
    assert updated is not None
    assert updated["effort"] == "high"


def test_edit_default_agent_succeeds(tmp_path, monkeypatch):
    """Delete-protected, but editing the default agent is allowed."""
    from lionagi.studio.services.agents import DEFAULT_AGENT_NAME, create_agent, update_agent

    _make_agents_root(tmp_path, monkeypatch)
    create_agent(DEFAULT_AGENT_NAME, {"provider": "claude", "model": "claude-sonnet-4-6"})

    updated = update_agent(DEFAULT_AGENT_NAME, {"effort": "medium"})
    assert updated is not None
    assert updated["effort"] == "medium"


# HTTP route wiring (protection status codes)


def _make_patched_client(tmp_path, monkeypatch):
    import lionagi.studio.services.agents as agents_mod

    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", root)

    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    return (
        TestClient(
            app,
            base_url="http://127.0.0.1:8765",
            headers={"Content-Type": "application/json"},
        ),
        root,
    )


def test_route_create_then_duplicate_conflicts(tmp_path, monkeypatch):
    client, _ = _make_patched_client(tmp_path, monkeypatch)

    r1 = client.post(
        "/api/agents/route-agent", json={"provider": "claude", "model": "claude-sonnet-4-6"}
    )
    assert r1.status_code == 200, r1.text

    r2 = client.post(
        "/api/agents/route-agent", json={"provider": "claude", "model": "claude-sonnet-4-6"}
    )
    assert r2.status_code == 409, r2.text


def test_route_delete_system_agent_is_403(tmp_path, monkeypatch):
    client, root = _make_patched_client(tmp_path, monkeypatch)
    _write_agent_md(
        root / "sysagent.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: true
        ---
        System prompt.
        """,
    )

    r = client.delete("/api/agents/sysagent")
    assert r.status_code == 403, r.text


def test_route_delete_ordinary_agent_is_200(tmp_path, monkeypatch):
    client, _ = _make_patched_client(tmp_path, monkeypatch)
    client.post(
        "/api/agents/route-throwaway", json={"provider": "claude", "model": "claude-sonnet-4-6"}
    )

    r = client.delete("/api/agents/route-throwaway")
    assert r.status_code == 200, r.text

    r2 = client.get("/api/agents/route-throwaway")
    assert r2.status_code == 404


# The generic /definitions/agent/{name} save route is the other write path onto
# agent files (Studio's own AgentDetail editor uses it, not PUT /agents/{name}).
# It must honour the same "system agent is not editable" rule or the protection
# added above is a no-op in practice.


def _make_definitions_client(tmp_path, monkeypatch):
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.agents as agents_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    agents_dir = fake_home / "agents"
    playbooks_dir = fake_home / "playbooks"
    agents_dir.mkdir()
    playbooks_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", playbooks_dir)
    monkeypatch.setattr(defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": playbooks_dir})
    # Keep agents.py's own root in sync so both write paths agree in the test.
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", agents_dir)

    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765"), agents_dir


def test_definitions_save_route_refuses_system_agent(tmp_path, monkeypatch):
    client, agents_dir = _make_definitions_client(tmp_path, monkeypatch)
    _write_agent_md(
        agents_dir / "sysagent.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: true
        ---
        Original prompt.
        """,
    )

    r = client.post("/api/definitions/agent/sysagent", json={"content": "# hijacked"})
    assert r.status_code == 403, r.text
    assert "Original prompt." in (agents_dir / "sysagent.md").read_text()


def test_definitions_save_route_refuses_quoted_truthy_lion_system(tmp_path, monkeypatch):
    """Same quoted-string case as the PUT/DELETE guards, for the generic definitions
    save path -- it must resolve the same predicate, not a hand-rolled copy of it."""
    client, agents_dir = _make_definitions_client(tmp_path, monkeypatch)
    _write_agent_md(
        agents_dir / "quotedsys.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: "true"
        ---
        Original prompt.
        """,
    )

    r = client.post("/api/definitions/agent/quotedsys", json={"content": "# hijacked"})
    assert r.status_code == 403, r.text
    assert "Original prompt." in (agents_dir / "quotedsys.md").read_text()


def test_definitions_save_route_allows_ordinary_agent(tmp_path, monkeypatch):
    client, agents_dir = _make_definitions_client(tmp_path, monkeypatch)
    _write_agent_md(
        agents_dir / "useragent.md",
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: false
        ---
        Original prompt.
        """,
    )

    r = client.post("/api/definitions/agent/useragent", json={"content": "# updated"})
    assert r.status_code == 200, r.text
    assert (agents_dir / "useragent.md").read_text().strip() == "# updated"


# The definitions save route also has to run the same cast role/mode
# validation POST/PUT /api/agents/{name} apply -- otherwise a payload the
# agents API rejects can still land on disk through the raw markdown door.


def test_definitions_save_route_rejects_unknown_role(tmp_path, monkeypatch):
    client, agents_dir = _make_definitions_client(tmp_path, monkeypatch)

    content = (
        "---\nprovider: claude\nmodel: claude-sonnet-4-6\nrole: not-a-real-role\n---\n\nBody.\n"
    )
    r = client.post("/api/definitions/agent/bogus-role", json={"content": content})

    assert r.status_code == 422, r.text
    assert not (agents_dir / "bogus-role.md").exists()


def test_definitions_save_route_rejects_unknown_mode(tmp_path, monkeypatch):
    client, agents_dir = _make_definitions_client(tmp_path, monkeypatch)

    content = (
        "---\nprovider: claude\nmodel: claude-sonnet-4-6\nmode: not-a-real-mode\n---\n\nBody.\n"
    )
    r = client.post("/api/definitions/agent/bogus-mode", json={"content": content})

    assert r.status_code == 422, r.text
    assert not (agents_dir / "bogus-mode.md").exists()


def test_definitions_save_route_rejects_same_payload_agents_api_rejects(tmp_path, monkeypatch):
    """The exact role value POST /api/agents/{name} rejects must also be
    rejected via the definitions route, with the same error class (422) --
    otherwise the definitions door is a bypass for the agents door's guard."""
    client, _ = _make_definitions_client(tmp_path, monkeypatch)

    r_agents = client.post(
        "/api/agents/via-agents-api",
        json={"provider": "claude", "model": "claude-sonnet-4-6", "role": "not-a-real-role"},
    )
    assert r_agents.status_code == 422, r_agents.text

    content = (
        "---\nprovider: claude\nmodel: claude-sonnet-4-6\nrole: not-a-real-role\n---\n\nBody.\n"
    )
    r_defs = client.post("/api/definitions/agent/via-definitions-api", json={"content": content})
    assert r_defs.status_code == 422, r_defs.text


def test_definitions_save_route_allows_valid_role_and_mode(tmp_path, monkeypatch):
    """A payload that would be accepted by the agents API still saves through
    the definitions route -- the new guard must not reject valid casts."""
    from lionagi.casts.pattern import list_modes, list_roles

    client, agents_dir = _make_definitions_client(tmp_path, monkeypatch)
    role = list_roles()[0]
    mode = list_modes()[0]

    content = f"---\nprovider: claude\nmodel: claude-sonnet-4-6\nrole: {role}\nmode: {mode}\n---\n\nBody.\n"
    r = client.post("/api/definitions/agent/valid-cast", json={"content": content})

    assert r.status_code == 200, r.text
    assert (agents_dir / "valid-cast.md").read_text() == content


def test_definitions_save_route_ignores_validation_for_non_agent_kind(tmp_path, monkeypatch):
    """role/mode validation is agent-specific; a playbook save must not be
    affected even though its content also happens to mention 'role'."""
    client, _ = _make_definitions_client(tmp_path, monkeypatch)

    r = client.post(
        "/api/definitions/playbook/some-playbook",
        json={"content": "role: not-a-real-role\nsteps: []\n"},
    )
    assert r.status_code == 200, r.text
