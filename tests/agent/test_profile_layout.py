# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent profile directory reorg.

Verifies both layouts work:
  - .lionagi/agents/<name>/<name>.md  (directory, preferred)
  - .lionagi/agents/<name>.md         (flat, legacy backward-compat)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.cli._agents import _resolve_profile_path, list_agents, load_agent_profile

# ── _resolve_profile_path ────────────────────────────────────────────


def test_resolver_prefers_directory_layout(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    (agents_dir / "bob").mkdir(parents=True)
    dir_path = agents_dir / "bob" / "bob.md"
    dir_path.write_text("from dir\n")
    flat_path = agents_dir / "bob.md"
    flat_path.write_text("from flat\n")

    assert _resolve_profile_path(agents_dir, "bob") == dir_path


def test_resolver_falls_back_to_flat(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    flat_path = agents_dir / "alice.md"
    flat_path.write_text("from flat\n")

    assert _resolve_profile_path(agents_dir, "alice") == flat_path


def test_resolver_returns_none_when_missing(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    assert _resolve_profile_path(agents_dir, "ghost") is None


# ── Full loader behaviour via HOME override ──────────────────────────


@pytest.fixture
def isolated_home(monkeypatch, tmp_path: Path) -> Path:
    """Point HOME at a scratch dir, and cd into it so _find_lionagi_dirs
    doesn't walk up into a real .lionagi/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_load_agent_profile_dir_layout(isolated_home: Path) -> None:
    profile_dir = isolated_home / ".lionagi" / "agents" / "orchestrator"
    profile_dir.mkdir(parents=True)
    (profile_dir / "orchestrator.md").write_text(
        "---\nmodel: claude-code/opus-4-7\neffort: high\n---\n\nYou orchestrate.\n"
    )

    profile = load_agent_profile("orchestrator")
    assert profile.name == "orchestrator"
    assert profile.model == "claude-code/opus-4-7"
    assert profile.effort == "high"
    assert "You orchestrate." in profile.system_prompt


def test_load_agent_profile_flat_layout_still_works(
    isolated_home: Path,
) -> None:
    agents_dir = isolated_home / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "legacy.md").write_text("---\nmodel: codex/gpt-5.4\n---\n\nLegacy profile.\n")

    profile = load_agent_profile("legacy")
    assert profile.name == "legacy"
    assert profile.model == "codex/gpt-5.4"


def test_load_agent_profile_dir_beats_flat(isolated_home: Path) -> None:
    agents_dir = isolated_home / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "both.md").write_text("---\nmodel: flat\n---\n\nflat body.\n")
    dir_path = agents_dir / "both"
    dir_path.mkdir()
    (dir_path / "both.md").write_text("---\nmodel: dir\n---\n\ndir body.\n")

    profile = load_agent_profile("both")
    assert profile.model == "dir"
    assert "dir body" in profile.system_prompt


def test_list_agents_discovers_both_layouts(isolated_home: Path) -> None:
    agents_dir = isolated_home / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    # Flat
    (agents_dir / "flat_one.md").write_text("x\n")
    (agents_dir / "flat_two.md").write_text("x\n")
    # Directory
    for name in ("dir_one", "dir_two"):
        d = agents_dir / name
        d.mkdir()
        (d / f"{name}.md").write_text("x\n")
    # Directory without matching main file — not discovered
    (agents_dir / "incomplete").mkdir()

    names = list_agents()
    assert set(names) >= {"flat_one", "flat_two", "dir_one", "dir_two"}
    assert "incomplete" not in names


def test_load_missing_profile_lists_available(isolated_home: Path) -> None:
    agents_dir = isolated_home / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    d = agents_dir / "alpha"
    d.mkdir()
    (d / "alpha.md").write_text("x\n")

    with pytest.raises(FileNotFoundError) as excinfo:
        load_agent_profile("zeta")
    assert "alpha" in str(excinfo.value)


def test_supplementary_files_coexist_with_main(isolated_home: Path) -> None:
    """Agent directory can hold additional reference files alongside the main."""
    profile_dir = isolated_home / ".lionagi" / "agents" / "orchestrator"
    profile_dir.mkdir(parents=True)
    (profile_dir / "orchestrator.md").write_text("---\nmodel: x\n---\n\nmain body\n")
    (profile_dir / "patterns").mkdir()
    (profile_dir / "patterns" / "empaco.md").write_text("# empaco\n")
    (profile_dir / "refs").mkdir()
    (profile_dir / "refs" / "commit.md").write_text("# commit\n")

    # Loader ignores supplementary files — only main profile is parsed.
    profile = load_agent_profile("orchestrator")
    assert "main body" in profile.system_prompt
    assert "empaco" not in profile.system_prompt
    # But the supplementary tree remains readable.
    assert (profile_dir / "patterns" / "empaco.md").is_file()
    assert (profile_dir / "refs" / "commit.md").is_file()
