# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the skill editor: validate_skill_content(), and definitions.py's
skill-kind save/get/rollback path (issue: skills can be viewed but not
edited or saved)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.studio.services.skills import validate_skill_content  # noqa: E402

# validate_skill_content() — both arms per finding

VALID_SKILL = """---
name: my-skill
description: Does a thing.
allowed-tools:
  - Read
  - Bash
---
Body text.
"""


def test_validate_skill_content_accepts_well_formed_skill():
    errors = validate_skill_content(VALID_SKILL, "my-skill")
    assert errors == []


def test_validate_skill_content_rejects_broken_yaml_frontmatter():
    broken = "---\nname: [unterminated\n---\nBody.\n"
    errors = validate_skill_content(broken, "my-skill")
    assert errors
    assert any("YAML" in e for e in errors)


def test_validate_skill_content_rejects_non_mapping_frontmatter():
    # A YAML list, not a mapping, at the top of the frontmatter block.
    content = "---\n- one\n- two\n---\nBody.\n"
    errors = validate_skill_content(content, "my-skill")
    assert errors
    assert any("mapping" in e for e in errors)


def test_validate_skill_content_rejects_non_list_allowed_tools():
    content = "---\nname: x\nallowed-tools: Bash\n---\nBody.\n"
    errors = validate_skill_content(content, "x")
    assert errors
    assert any("allowed-tools" in e for e in errors)


def test_validate_skill_content_rejects_allowed_tools_with_non_string_entries():
    content = "---\nname: x\nallowed-tools: [Bash, 5]\n---\nBody.\n"
    errors = validate_skill_content(content, "x")
    assert errors
    assert any("allowed-tools" in e for e in errors)


def test_validate_skill_content_accepts_missing_allowed_tools():
    content = "---\nname: x\ndescription: y\n---\nBody.\n"
    errors = validate_skill_content(content, "x")
    assert errors == []


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "a\\b", "", ".", ".."])
def test_validate_skill_content_rejects_unsafe_names(bad_name):
    errors = validate_skill_content(VALID_SKILL, bad_name)
    assert errors
    assert any("skill name" in e for e in errors)


def test_validate_skill_content_accepts_bare_identifier_name():
    errors = validate_skill_content(VALID_SKILL, "my-skill_2")
    assert errors == []


def test_validate_skill_content_rejects_missing_closing_delimiter():
    """An opening --- with no closing --- must not be accepted as "no
    frontmatter" -- it's malformed content, not absent content."""
    content = "---\nname: valid\nBody without closing delimiter"
    errors = validate_skill_content(content, "my-skill")
    assert errors
    assert any("YAML" in e for e in errors)


def test_validate_skill_content_rejects_null_frontmatter():
    """An explicit YAML null document must not be coerced into valid, empty
    metadata."""
    content = "---\nnull\n---\nBody.\n"
    errors = validate_skill_content(content, "my-skill")
    assert errors
    assert any("YAML" in e or "mapping" in e for e in errors)


# definitions.py — skill kind save / get / rollback

from tests.apps_studio_server._helpers import run_async as _run  # noqa: E402


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_skill_definition_writes_canonical_skill_md_shape(tmp_path, monkeypatch):
    """Saving a skill through the definitions system must always land at
    <name>/SKILL.md -- the shape lionagi/cli/skill.py's resolve_skill_path()
    actually looks for -- never a bare <name>.md."""
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "SKILLS_DIR", skills_dir)

    content = "---\nname: my-skill\ndescription: does a thing\n---\nBody text.\n"
    result = await defs_mod.save_definition("skill", "my-skill", content, "initial save")

    assert result["kind"] == "skill"
    assert result["version"] == 1

    skill_md = skills_dir / "my-skill" / "SKILL.md"
    assert skill_md.exists()
    assert skill_md.read_text() == content
    assert not (skills_dir / "my-skill.md").exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_skill_definition_normalizes_legacy_bare_md_shape(tmp_path, monkeypatch):
    """An existing skill saved in the legacy bare <name>.md shape still gets
    read correctly, and a save through Studio normalizes it to <name>/SKILL.md
    rather than overwriting the bare file in place."""
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "SKILLS_DIR", skills_dir)

    legacy = skills_dir / "legacy-skill.md"
    legacy.write_text("---\nname: legacy-skill\n---\nOld body.\n")

    # Reading resolves the legacy shape.
    fetched = await defs_mod.get_definition("skill", "legacy-skill")
    assert fetched is not None
    assert "Old body." in fetched["content"]

    # Saving normalizes to the canonical directory shape.
    new_content = "---\nname: legacy-skill\n---\nNew body.\n"
    await defs_mod.save_definition("skill", "legacy-skill", new_content, "normalize")

    canonical = skills_dir / "legacy-skill" / "SKILL.md"
    assert canonical.exists()
    assert canonical.read_text() == new_content
    # The old bare file is left in place (untouched), not deleted out from
    # under anything else that might still reference it, but the canonical
    # path is now what both Studio and `li skill` resolve.
    assert legacy.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_skill_definition_rejects_broken_frontmatter(tmp_path, monkeypatch):
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "SKILLS_DIR", skills_dir)

    broken = "---\nname: [unterminated\n---\nBody.\n"
    with pytest.raises(ValueError, match="YAML"):
        await defs_mod.save_definition("skill", "bad-skill", broken)

    assert not (skills_dir / "bad-skill").exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_skill_definition_rejects_missing_closing_delimiter(tmp_path, monkeypatch):
    """The save endpoint must reject an opening --- with no closing --- as
    malformed content, not silently store it as a skill with no metadata."""
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "SKILLS_DIR", skills_dir)

    unterminated = "---\nname: valid\nBody without closing delimiter"
    with pytest.raises(ValueError, match="YAML"):
        await defs_mod.save_definition("skill", "bad-skill-2", unterminated)

    assert not (skills_dir / "bad-skill-2").exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_skill_definition_rejects_null_frontmatter(tmp_path, monkeypatch):
    """The save endpoint must reject an explicit YAML null frontmatter
    document instead of coercing it into valid, empty metadata."""
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "SKILLS_DIR", skills_dir)

    null_fm = "---\nnull\n---\nBody.\n"
    with pytest.raises(ValueError):
        await defs_mod.save_definition("skill", "bad-skill-3", null_fm)

    assert not (skills_dir / "bad-skill-3").exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_skill_definition_increments_version_and_supports_rollback(
    tmp_path, monkeypatch
):
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "SKILLS_DIR", skills_dir)

    v1 = "---\nname: s\n---\nv1 body.\n"
    v2 = "---\nname: s\n---\nv2 body.\n"
    r1 = await defs_mod.save_definition("skill", "s", v1)
    r2 = await defs_mod.save_definition("skill", "s", v2)
    assert r1["version"] == 1
    assert r2["version"] == 2

    current = await defs_mod.get_definition("skill", "s")
    assert current["content"] == v2
    assert current["version"] == 2
    assert len(current["versions"]) == 2

    rolled_back = await defs_mod.rollback_definition("skill", "s", 1)
    assert rolled_back is not None
    assert rolled_back["version"] == 3

    after_rollback = await defs_mod.get_definition("skill", "s")
    assert after_rollback["content"] == v1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_skill_definition_returns_none_for_unknown_skill(tmp_path, monkeypatch):
    import lionagi.cli._runs as cli_runs_mod
    import lionagi.studio.services.definitions as defs_mod

    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()

    monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
    monkeypatch.setattr(defs_mod, "SKILLS_DIR", skills_dir)

    result = await defs_mod.get_definition("skill", "does-not-exist")
    assert result is None
