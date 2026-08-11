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
NESTED_SECRET = "NESTED-SECRET-4e2a91"
LIST_SECRET = "LIST-SECRET-7b3c05"

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


FIXTURE_NESTED_SECRET_AGENT_MD = f"""\
---
provider: claude
model: claude-sonnet-4-6
role:
  leaked: {NESTED_SECRET}
effort:
  - {LIST_SECRET}
permission_mode: default
lion_system: false
---

body text
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

    return (
        TestClient(
            app,
            base_url="http://127.0.0.1:8765",
            headers={"Content-Type": "application/json"},
        ),
        agents_dir,
    )


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
# A safe key's name is not a promise about its shape. A mapping or list
# smuggled in under a safe key (role/effort/...) must be dropped, not passed
# through by name match alone -- on every path that reads the allowlist.
# ---------------------------------------------------------------------------


def test_nested_value_under_safe_key_is_dropped_not_passed_through(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "nestedagent.md", FIXTURE_NESTED_SECRET_AGENT_MD)

    # Must-MATCH arm: with the switch off, every route that surfaces the raw
    # frontmatter still carries the nested content.
    normal_detail = client.get("/api/agents/nestedagent")
    normal_list = client.get("/api/agents/")
    normal_definition = client.get("/api/definitions/agent/nestedagent")
    assert normal_detail.status_code == 200, normal_detail.text
    assert normal_list.status_code == 200, normal_list.text
    assert normal_definition.status_code == 200, normal_definition.text
    assert NESTED_SECRET in normal_detail.text
    assert LIST_SECRET in normal_detail.text
    assert NESTED_SECRET in normal_list.text
    assert LIST_SECRET in normal_list.text
    assert NESTED_SECRET in normal_definition.text
    assert LIST_SECRET in normal_definition.text

    # Must-NOT-match arm: with the switch on, a mapping under "role" and a
    # list under "effort" are dropped rather than passed through because
    # their key names happen to match the safe-key allowlist.
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted_detail = client.get("/api/agents/nestedagent")
    redacted_list = client.get("/api/agents/")
    redacted_definition = client.get("/api/definitions/agent/nestedagent")
    assert redacted_detail.status_code == 200, redacted_detail.text
    assert redacted_list.status_code == 200, redacted_list.text
    assert redacted_definition.status_code == 200, redacted_definition.text
    for resp in (redacted_detail, redacted_list, redacted_definition):
        assert NESTED_SECRET not in resp.text
        assert LIST_SECRET not in resp.text


def test_mcp_list_agents_drops_nested_secret_under_safe_key(tmp_path, monkeypatch):
    """The Operator's MCP agent listing routes through the same allowlist as
    the HTTP routes -- a row shaped with a nested value under a safe key
    (whatever produced it) must not leak through this surface either."""
    import lionagi.studio.services.agents as agents_service
    from lionagi.studio.operator import application_mcp

    from ._helpers import run_async

    _make_redaction_client(tmp_path, monkeypatch)
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    monkeypatch.setattr(
        agents_service,
        "list_agents",
        lambda: [
            {
                "name": "demoagent",
                "provider": {"leaked": NESTED_SECRET},
                "model": "claude-sonnet-4-6",
                "description": "fine",
            }
        ],
    )

    result = run_async(application_mcp.list_agents({"limit": 10}))
    assert result["agents"][0]["name"] == "demoagent"
    assert result["agents"][0]["provider"] is None
    assert result["agents"][0]["model"] == "claude-sonnet-4-6"
    assert NESTED_SECRET not in str(result)


# ---------------------------------------------------------------------------
# POST /agents/{name} and PUT /agents/{name} must not return the raw service
# result in demo mode -- a harmless metadata edit must not be a side door to
# the full prompt/guidance the GET routes already redact.
# ---------------------------------------------------------------------------


def test_create_and_update_agent_responses_are_redacted_in_demo_mode(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)

    # Must-MATCH arm: with the switch off, both responses still carry the
    # real content.
    create_resp = client.post(
        "/api/agents/newagent",
        json={
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "system_prompt": PROMPT_SENTINEL,
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    assert PROMPT_SENTINEL in create_resp.text

    put_resp = client.put("/api/agents/newagent", json={"system_prompt": PROMPT_SENTINEL + "-v2"})
    assert put_resp.status_code == 200, put_resp.text
    assert (PROMPT_SENTINEL + "-v2") in put_resp.text

    # Must-NOT-match arm: with the switch on, a metadata-only PUT (no
    # system_prompt in the request body) must not echo back the existing
    # prompt in full, and a POST creating a new agent with real content must
    # not return that content either.
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    metadata_put = client.put(
        "/api/agents/newagent", json={"description": "harmless metadata edit"}
    )
    assert metadata_put.status_code == 200, metadata_put.text
    assert PROMPT_SENTINEL not in metadata_put.text
    assert "<redacted," in metadata_put.text

    create_resp2 = client.post(
        "/api/agents/newagent2",
        json={
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "system_prompt": PROMPT_SENTINEL,
        },
    )
    assert create_resp2.status_code == 200, create_resp2.text
    assert PROMPT_SENTINEL not in create_resp2.text
    assert "<redacted," in create_resp2.text

    # Response-only redaction -- the real content is still on disk.
    assert (PROMPT_SENTINEL + "-v2") in (agents_dir / "newagent.md").read_text()
    assert PROMPT_SENTINEL in (agents_dir / "newagent2.md").read_text()


# ---------------------------------------------------------------------------
# The plugin-agent route is a full-content mirror of the same kind of
# owner-authored markdown the Library agent routes protect -- it must not be
# a silent bypass one path segment over.
# ---------------------------------------------------------------------------


def test_plugin_agent_route_is_redacted_in_demo_mode(tmp_path, monkeypatch):
    import lionagi.studio.services.plugins as plugins_mod

    client, _agents_dir = _make_redaction_client(tmp_path, monkeypatch)

    plugin_dir = tmp_path / "fakeplugin"
    plugin_agents_dir = plugin_dir / "agents"
    plugin_agents_dir.mkdir(parents=True)
    (plugin_agents_dir / "critic.md").write_text(
        f"---\ndescription: plugin critic\n---\n\n{PROMPT_SENTINEL}\n"
    )
    monkeypatch.setattr(
        plugins_mod,
        "_find_plugin_dir_for",
        lambda name: plugin_dir if name == "fakeplugin" else None,
    )

    normal = client.get("/api/plugins/fakeplugin/agents/critic")
    assert normal.status_code == 200, normal.text
    assert PROMPT_SENTINEL in normal.text

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted = client.get("/api/plugins/fakeplugin/agents/critic")
    assert redacted.status_code == 200, redacted.text
    assert PROMPT_SENTINEL not in redacted.text
    assert "<redacted," in redacted.text


# ---------------------------------------------------------------------------
# The definitions listing carries the same path/disk_path fields the single
# get_definition() route already abbreviates -- the listing must match it
# instead of shipping the unabridged on-disk location for every agent.
# ---------------------------------------------------------------------------


def test_definitions_listing_path_is_projected_in_demo_mode(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "demoagent.md", FIXTURE_AGENT_MD)

    normal = client.get("/api/definitions/")
    assert normal.status_code == 200, normal.text
    entry = next(
        d for d in normal.json()["definitions"] if d["kind"] == "agent" and d["name"] == "demoagent"
    )
    # Must-MATCH arm: the unabridged path, not just the bare filename.
    assert entry["path"] != "demoagent.md"
    assert entry["path"].endswith("demoagent.md")
    assert entry["disk_path"] != "demoagent.md"
    assert entry["disk_path"].endswith("demoagent.md")

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted = client.get("/api/definitions/")
    assert redacted.status_code == 200, redacted.text
    r_entry = next(
        d
        for d in redacted.json()["definitions"]
        if d["kind"] == "agent" and d["name"] == "demoagent"
    )
    assert r_entry["path"] == "demoagent.md"
    assert r_entry["disk_path"] == "demoagent.md"


# ---------------------------------------------------------------------------
# rollback_definition() must write the real content back, not the redacted
# placeholder that get_version() shows an external caller -- a rollback is a
# write, and redaction applies to reads that leave the service.
# ---------------------------------------------------------------------------


def test_rollback_succeeds_with_real_content_in_demo_mode(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)

    v1 = client.post("/api/definitions/agent/rollme", json={"content": FIXTURE_AGENT_MD})
    assert v1.status_code == 200, v1.text
    v1_version = v1.json()["version"]

    updated_content = FIXTURE_AGENT_MD.replace(PROMPT_SENTINEL, PROMPT_SENTINEL + "-v2")
    v2 = client.post("/api/definitions/agent/rollme", json={"content": updated_content})
    assert v2.status_code == 200, v2.text

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    rollback = client.post("/api/definitions/agent/rollme/rollback", params={"version": v1_version})
    assert rollback.status_code == 200, rollback.text

    on_disk = (agents_dir / "rollme.md").read_text()
    assert PROMPT_SENTINEL in on_disk
    assert "<redacted," not in on_disk


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
# /api/plugins/{name} embeds each agent's {name, description} and the
# plugin's on-disk path directly -- it must project both through the same
# table the dedicated /plugins/{plugin}/agents/{agent} route already applies,
# not mirror them unfiltered one path segment over.
# ---------------------------------------------------------------------------


def test_plugin_detail_route_redacts_nested_agents_and_path(tmp_path, monkeypatch):
    import lionagi.studio.services.plugins as plugins_mod

    client, _agents_dir = _make_redaction_client(tmp_path, monkeypatch)

    plugin_dir = tmp_path / "fakeplugin3"
    plugin_agents_dir = plugin_dir / "agents"
    plugin_agents_dir.mkdir(parents=True)
    (plugin_agents_dir / "critic.md").write_text(
        f"---\ndescription: {DESCRIPTION_SENTINEL}\n---\n\nbody\n"
    )
    monkeypatch.setattr(
        plugins_mod,
        "_iter_marketplace_plugins",
        lambda: [(plugin_dir, "fakeplugin3", "a fake plugin")],
    )
    monkeypatch.setattr(plugins_mod, "_iter_thirdparty_plugins", lambda: [])
    # A deterministic, multi-segment path so the redacted response is
    # distinguishable from a bare plugin-directory name either way.
    monkeypatch.setattr(plugins_mod, "public_path", lambda p, **kw: f"marketplace/{p.name}/nested")

    # Must-MATCH arm: with the switch off, both the agent description and the
    # full on-disk path are served as-is.
    normal = client.get("/api/plugins/fakeplugin3")
    assert normal.status_code == 200, normal.text
    assert DESCRIPTION_SENTINEL in normal.text
    assert normal.json()["path"] == "marketplace/fakeplugin3/nested"

    # Must-NOT-match arm: with the switch on, the embedded agent's
    # description is redacted and the path is abbreviated to a bare name.
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted = client.get("/api/plugins/fakeplugin3")
    assert redacted.status_code == 200, redacted.text
    assert DESCRIPTION_SENTINEL not in redacted.text
    body = redacted.json()
    assert body["agents"][0]["name"] == "critic"
    assert body["path"] == "nested"


# ---------------------------------------------------------------------------
# /api/plugins (the list route) builds each entry from the same summary the
# detail route abbreviates -- it must agree with /api/plugins/{name} instead
# of shipping the unabridged on-disk path one route over.
# ---------------------------------------------------------------------------


def test_plugin_list_route_redacts_path_matching_detail_route(tmp_path, monkeypatch):
    import lionagi.studio.services.plugins as plugins_mod

    client, _agents_dir = _make_redaction_client(tmp_path, monkeypatch)

    plugin_dir = tmp_path / "fakeplugin4"
    plugin_dir.mkdir(parents=True)
    monkeypatch.setattr(
        plugins_mod,
        "_iter_marketplace_plugins",
        lambda: [(plugin_dir, "fakeplugin4", "a fake plugin")],
    )
    monkeypatch.setattr(plugins_mod, "_iter_thirdparty_plugins", lambda: [])
    monkeypatch.setattr(plugins_mod, "public_path", lambda p, **kw: f"marketplace/{p.name}/nested")

    # Must-MATCH arm: with the switch off, the list route serves the full path.
    normal = client.get("/api/plugins")
    assert normal.status_code == 200, normal.text
    normal_entry = next(p for p in normal.json()["plugins"] if p["name"] == "fakeplugin4")
    assert normal_entry["path"] == "marketplace/fakeplugin4/nested"
    assert normal_entry["agent_count"] == 0

    # Must-NOT-match arm: with the switch on, the list route abbreviates the
    # path the same way the detail route already does.
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted = client.get("/api/plugins")
    assert redacted.status_code == 200, redacted.text
    redacted_entry = next(p for p in redacted.json()["plugins"] if p["name"] == "fakeplugin4")
    assert redacted_entry["path"] == "nested"
    # Topology fields are untouched -- this route never leaked agent content,
    # only the filesystem path.
    assert redacted_entry["agent_count"] == 0

    detail = client.get("/api/plugins/fakeplugin4")
    assert detail.status_code == 200, detail.text
    assert detail.json()["path"] == redacted_entry["path"]


# ---------------------------------------------------------------------------
# A scalar-shaped safe key's guard is only as good as the value it inspects.
# list_agents()/get_agent() used to str()-coerce provider/model *before* the
# classification table saw them, so a nested mapping smuggled in under
# `provider` in real YAML frontmatter became a plain string the table's
# scalar check waved through. This plants the nested value in an actual
# on-disk agent file (not a monkeypatched, already-dict-shaped MCP row) so
# the service layer's own coercion is what's under test.
# ---------------------------------------------------------------------------


def test_nested_provider_from_real_yaml_frontmatter_is_dropped(tmp_path, monkeypatch):
    from lionagi.studio.operator import application_mcp

    from ._helpers import run_async

    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(
        agents_dir / "nestedprovider.md",
        f"""\
        ---
        provider:
          leaked: {NESTED_SECRET}
        model: claude-sonnet-4-6
        lion_system: false
        ---

        body text
        """,
    )

    normal_list = client.get("/api/agents/")
    assert NESTED_SECRET in normal_list.text

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted_list = client.get("/api/agents/")
    redacted_detail = client.get("/api/agents/nestedprovider")
    assert redacted_list.status_code == 200, redacted_list.text
    assert redacted_detail.status_code == 200, redacted_detail.text
    assert NESTED_SECRET not in redacted_list.text
    assert NESTED_SECRET not in redacted_detail.text

    entry = next(a for a in redacted_list.json()["agents"] if a["name"] == "nestedprovider")
    assert entry.get("provider") is None
    assert redacted_detail.json().get("provider") is None

    # Same real file, read through the Operator's MCP roster.
    result = run_async(application_mcp.list_agents({"limit": 10}))
    assert NESTED_SECRET not in str(result)
    row = next(a for a in result["agents"] if a["name"] == "nestedprovider")
    assert row["provider"] is None


# ---------------------------------------------------------------------------
# abbreviate_path() must refuse to str()-serialize a non-path-like value
# rather than smuggle its content through as a "filename" -- a `path:` key
# collides with the reserved field project_agent_fields adds for every
# profile record.
# ---------------------------------------------------------------------------


def test_abbreviate_path_rejects_non_path_like_value():
    from lionagi.studio.services.redaction import abbreviate_path

    with pytest.raises(TypeError):
        abbreviate_path({"leaked": "x"})
    with pytest.raises(TypeError):
        abbreviate_path(["x"])
    assert abbreviate_path("some/dir/file.md") == "file.md"


def test_malformed_path_frontmatter_value_is_dropped_not_serialized(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(
        agents_dir / "pathagent.md",
        f"""\
        ---
        provider: claude
        model: claude-sonnet-4-6
        path:
          leaked: {NESTED_SECRET}
        lion_system: false
        ---

        body text
        """,
    )

    # Must-MATCH arm: with the switch off, the raw file (including its
    # colliding `path:` frontmatter key) is served as-is.
    normal_definition = client.get("/api/definitions/agent/pathagent")
    assert normal_definition.status_code == 200, normal_definition.text
    assert NESTED_SECRET in normal_definition.text

    # Must-NOT-match arm: with the switch on, the malformed path value is
    # dropped rather than str()-serialized into the response.
    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    redacted_definition = client.get("/api/definitions/agent/pathagent")
    assert redacted_definition.status_code == 200, redacted_definition.text
    assert NESTED_SECRET not in redacted_definition.text


# ---------------------------------------------------------------------------
# snapshot_current() re-saves any definition whose disk content differs from
# its latest stored version. The internal equality check must compare
# against the raw stored content, not get_version()'s redacted response text
# -- otherwise an unchanged agent file in demo mode never matches its own
# redacted-placeholder comparison and gets re-snapshotted on every call.
# ---------------------------------------------------------------------------


def test_snapshot_current_does_not_resnapshot_unchanged_agent_in_demo_mode(tmp_path, monkeypatch):
    client, agents_dir = _make_redaction_client(tmp_path, monkeypatch)
    _write_agent_md(agents_dir / "snapagent.md", FIXTURE_AGENT_MD)

    first = client.post("/api/definitions/snapshot", params={"kind": "agent"})
    assert first.status_code == 200, first.text
    assert first.json()["snapshots_created"] == 1

    monkeypatch.setenv("LIONAGI_STUDIO_DEMO_MODE", "true")
    second = client.post("/api/definitions/snapshot", params={"kind": "agent"})
    assert second.status_code == 200, second.text
    assert second.json()["snapshots_created"] == 0

    current = client.get("/api/definitions/agent/snapagent")
    assert current.status_code == 200, current.text
    assert current.json()["version"] == 1


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
