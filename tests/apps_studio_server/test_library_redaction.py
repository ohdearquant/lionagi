# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Server-side redaction for a demo-safe Library view (LIONAGI_STUDIO_DEMO_MODE).

Every test plants distinct sentinel strings in a fixture agent profile and checks
both directions in the same assertion pass wherever practical: the normal
(switch-off) view must still serve the sentinel (a redaction bug that always
hides content would pass a leak-only test too), and the redacted (switch-on)
view must never serve it.
"""

from __future__ import annotations

import textwrap

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("yaml", reason="PyYAML not installed")

PROMPT_SENTINEL = "PROMPT-SENTINEL-4f2c9d17"
GUIDANCE_SENTINEL = "GUIDANCE-SENTINEL-9a1b62"
DESCRIPTION_SENTINEL = "DESCRIPTION-SENTINEL-77eecb"
SECRET_ENV_VALUE = "sk-ENV-SHAPED-SECRET-ab12cd34"

FIXTURE_AGENT_MD = f"""\
---
provider: claude
model: claude-sonnet-4-6
role: critic
effort: high
permission_mode: default
guidance: {GUIDANCE_SENTINEL}
description: {DESCRIPTION_SENTINEL}
internal_api_key: {SECRET_ENV_VALUE}
lion_system: false
---

{PROMPT_SENTINEL}
"""


def _write_agent_md(path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


def _make_redaction_client(tmp_path, monkeypatch):
    """A TestClient wired to a scratch LIONAGI_HOME, both agent write paths
    (PUT /agents/{name} and POST /definitions/agent/{name}) kept in sync, and
    the demo-mode switch guaranteed off until a test opts in."""
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
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", agents_dir)
    monkeypatch.delenv("LIONAGI_STUDIO_DEMO_MODE", raising=False)

    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765"), agents_dir


# ---------------------------------------------------------------------------
# Prompt body: leaks in the normal view, redacted in the demo view.
# ---------------------------------------------------------------------------


def test_redacted_view_hides_prompt_body_normal_view_still_serves_it(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)

    # Must-MATCH arm: with the switch off, both routes that render the profile
    # detail still serve the real prompt body. A test that only asserted the
    # negative below would also pass for a route that always hides content.
    normal_detail = client.get("/api/agents/demoagent")
    normal_definition = client.get("/api/definitions/agent/demoagent")
    assert normal_detail.status_code == 200, normal_detail.text
    assert normal_definition.status_code == 200, normal_definition.text
    assert PROMPT_SENTINEL in normal_detail.text
    assert PROMPT_SENTINEL in normal_definition.text

    # Must-NOT-match arm: with the switch on, neither route leaks it.
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted_detail = client.get("/api/agents/demoagent")
    redacted_definition = client.get("/api/definitions/agent/demoagent")
    assert redacted_detail.status_code == 200, redacted_detail.text
    assert redacted_definition.status_code == 200, redacted_definition.text
    assert PROMPT_SENTINEL not in redacted_detail.text
    assert PROMPT_SENTINEL not in redacted_definition.text
    # The placeholder marker takes its place -- the field is present, not
    # silently dropped, so the UI can still show "content redacted".
    assert "<redacted," in redacted_detail.text
    assert "<redacted," in redacted_definition.text


# ---------------------------------------------------------------------------
# Unrecognized, env/secret-shaped frontmatter value: dropped by key name.
# ---------------------------------------------------------------------------


def test_env_shaped_frontmatter_value_is_masked_in_redacted_view(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)

    # list_agents() is the route that spreads arbitrary frontmatter keys onto
    # the response (get_agent() already only surfaces a known set); that's
    # the leak surface for an unrecognized, secret-shaped key.
    normal_list = client.get("/api/agents/")
    assert SECRET_ENV_VALUE in normal_list.text

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted_list = client.get("/api/agents/")
    redacted_detail = client.get("/api/agents/demoagent")
    redacted_definition = client.get("/api/definitions/agent/demoagent")
    assert SECRET_ENV_VALUE not in redacted_list.text
    assert SECRET_ENV_VALUE not in redacted_detail.text
    assert SECRET_ENV_VALUE not in redacted_definition.text

    entry = next(a for a in redacted_list.json()["agents"] if a["name"] == "demoagent")
    assert "internal_api_key" not in entry


# ---------------------------------------------------------------------------
# Safe-by-construction fields ride through unchanged in the redacted view.
# ---------------------------------------------------------------------------


def test_keep_fields_survive_redaction(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")

    list_entry = next(
        a for a in client.get("/api/agents/").json()["agents"] if a["name"] == "demoagent"
    )
    for field, expected in (
        ("name", "demoagent"),
        ("provider", "claude"),
        ("model", "claude-sonnet-4-6"),
        ("role", "critic"),
        ("effort", "high"),
        ("permission_mode", "default"),
    ):
        assert list_entry.get(field) == expected, list_entry

    detail = client.get("/api/agents/demoagent").json()
    for field, expected in (
        ("name", "demoagent"),
        ("provider", "claude"),
        ("model", "claude-sonnet-4-6"),
        ("role", "critic"),
        ("effort", "high"),
        ("permission_mode", "default"),
    ):
        assert detail.get(field) == expected, detail

    # Zero-curation fallback: the roster is still useful from safe fields
    # alone even though this profile's description was owner-authored text.
    assert DESCRIPTION_SENTINEL not in str(list_entry)


# ---------------------------------------------------------------------------
# A sibling route serving the same object (version history) must not be an
# unredacted mirror one click away from the covered detail route.
# ---------------------------------------------------------------------------


def test_version_history_route_is_also_redacted(tmp_path, monkeypatch):
    client, _agents_dir = _make_redaction_client(tmp_path, monkeypatch)

    save = client.post("/api/definitions/agent/versioned", json={"content": FIXTURE_AGENT_MD})
    assert save.status_code == 200, save.text
    version = save.json()["version"]

    normal_version = client.get(f"/api/definitions/agent/versioned/versions/{version}")
    assert normal_version.status_code == 200, normal_version.text
    assert PROMPT_SENTINEL in normal_version.text
    assert SECRET_ENV_VALUE in normal_version.text

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted_version = client.get(f"/api/definitions/agent/versioned/versions/{version}")
    assert redacted_version.status_code == 200, redacted_version.text
    assert PROMPT_SENTINEL not in redacted_version.text
    assert GUIDANCE_SENTINEL not in redacted_version.text
    assert SECRET_ENV_VALUE not in redacted_version.text


# ---------------------------------------------------------------------------
# The save path is not a bypass: a redacted payload posted back must be
# refused, and must not touch the file on disk, while the switch is on.
# ---------------------------------------------------------------------------


def test_save_definition_refuses_placeholder_payload_while_demo_mode_on(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)
    original_text = (agents_dir / "demoagent.md").read_text()

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    placeholder_payload = client.get("/api/definitions/agent/demoagent").json()["content"]
    assert "<redacted," in placeholder_payload

    r = client.post("/api/definitions/agent/demoagent", json={"content": placeholder_payload})
    assert r.status_code == 403, r.text
    assert (agents_dir / "demoagent.md").read_text() == original_text


def test_save_definition_refuses_empty_content_while_demo_mode_on(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)
    original_text = (agents_dir / "demoagent.md").read_text()

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    r = client.post("/api/definitions/agent/demoagent", json={"content": "   "})
    assert r.status_code == 403, r.text
    assert (agents_dir / "demoagent.md").read_text() == original_text


def test_put_agent_refuses_placeholder_system_prompt_while_demo_mode_on(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)
    original_text = (agents_dir / "demoagent.md").read_text()

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    placeholder_prompt = client.get("/api/agents/demoagent").json()["system_prompt"]
    assert "<redacted," in placeholder_prompt

    r = client.put("/api/agents/demoagent", json={"system_prompt": placeholder_prompt})
    assert r.status_code == 403, r.text
    assert (agents_dir / "demoagent.md").read_text() == original_text


def test_save_definition_still_works_normally_while_demo_mode_off(tmp_path, monkeypatch):
    """Negative control for the two refusal tests above: the guard is specific
    to demo mode, not a general block on saving agent definitions."""
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)

    r = client.post("/api/definitions/agent/demoagent", json={"content": "# updated content"})
    assert r.status_code == 200, r.text
    assert (agents_dir / "demoagent.md").read_text().strip() == "# updated content"


# ---------------------------------------------------------------------------
# Negative control: with the projection disabled (redact=False), the same
# routes DO leak -- confirming the tests above exercise the projection
# rather than passing independently of it.
# ---------------------------------------------------------------------------


def test_negative_control_projection_disabled_leaks_by_default(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)

    # Demo mode is off (the fixture's default) -- every route must leak.
    detail = client.get("/api/agents/demoagent")
    definition = client.get("/api/definitions/agent/demoagent")
    listing = client.get("/api/agents/")
    assert PROMPT_SENTINEL in detail.text
    assert PROMPT_SENTINEL in definition.text
    assert SECRET_ENV_VALUE in listing.text


# ---------------------------------------------------------------------------
# Route-enumeration coverage: fails loudly when a new route is registered
# under an area that reads agent-profile content without a redaction
# decision having been made for it.
# ---------------------------------------------------------------------------


def test_route_enumeration_covers_known_agent_profile_routes():
    from lionagi.studio.registry import iter_studio_routes, load_studio_route_modules

    load_studio_route_modules()

    agents_route_names = {r.name for r in iter_studio_routes(area="agents")}
    definitions_route_names = {r.name for r in iter_studio_routes(area="definitions")}

    expected_agents = {
        "list_agents",
        "get_agent",
        "create_agent",
        "update_agent",
        "delete_agent",
        None,  # /agents/{name}/validate is registered without an explicit name
    }
    expected_definitions = {
        "list_definitions",
        "get_definition",
        "get_version",
        "save_definition",
        "rollback_definition",
        "snapshot_current",
    }

    assert agents_route_names == expected_agents, (
        "A route was added or removed under area='agents'. If it reads "
        "agent-profile content, route it through "
        "redaction.project_agent_fields() and update this expected set; "
        "otherwise just update the set."
    )
    assert definitions_route_names == expected_definitions, (
        "A route was added or removed under area='definitions'. If it reads "
        "definition content for kind='agent', route it through "
        "redaction.redact_agent_markdown() and update this expected set."
    )
