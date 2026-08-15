# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for '-' / '_' equivalence in agent profile names, and its ambiguity guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.cli._providers import (
    AmbiguousProfileNameError,
    _resolve_profile_path,
    load_agent_profile,
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_hyphen_request_resolves_underscore_file(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    path = _write(agents_dir / "postmortem_lead.md", "x\n")

    assert _resolve_profile_path(agents_dir, "postmortem-lead") == path


def test_underscore_request_resolves_underscore_file(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    path = _write(agents_dir / "postmortem_lead.md", "x\n")

    assert _resolve_profile_path(agents_dir, "postmortem_lead") == path


def test_underscore_request_resolves_hyphen_file(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    path = _write(agents_dir / "postmortem-lead.md", "x\n")

    assert _resolve_profile_path(agents_dir, "postmortem_lead") == path


def test_hyphen_request_resolves_underscore_directory_layout(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    path = _write(agents_dir / "postmortem_lead" / "postmortem_lead.md", "x\n")

    assert _resolve_profile_path(agents_dir, "postmortem-lead") == path


def test_exact_spelling_wins_when_both_spellings_exist(tmp_path: Path) -> None:
    """A request naming an existing file resolves to it, never to the other spelling.

    A directory deliberately holding two profiles that differ only in separator
    resolved both before the spellings became interchangeable, and still does.
    Nothing is being silently ranked here: the caller named one of them.
    """
    agents_dir = tmp_path / "agents"
    hyphen = _write(agents_dir / "postmortem-lead.md", "hyphen\n")
    underscore = _write(agents_dir / "postmortem_lead.md", "underscore\n")

    assert _resolve_profile_path(agents_dir, "postmortem-lead") == hyphen
    assert _resolve_profile_path(agents_dir, "postmortem_lead") == underscore


def test_mixed_separator_request_with_no_exact_file_is_ambiguous(tmp_path: Path) -> None:
    """Ambiguity survives exactly where the request itself is ambiguous.

    The requested spelling does not exist, and normalizing it finds two
    different files. There is no named file to prefer, and resolving to either
    would make the other invisible.
    """
    agents_dir = tmp_path / "agents"
    underscored = _write(agents_dir / "postmortem_lead_v2.md", "underscore\n")
    hyphenated = _write(agents_dir / "postmortem-lead-v2.md", "hyphen\n")

    with pytest.raises(AmbiguousProfileNameError) as excinfo:
        _resolve_profile_path(agents_dir, "postmortem-lead_v2")
    message = str(excinfo.value)
    assert "postmortem-lead_v2" in message
    assert str(underscored) in message
    assert str(hyphenated) in message


def test_one_spelling_in_both_layouts_is_not_ambiguous(tmp_path: Path) -> None:
    """Directory layout still beats flat layout for the same spelling."""
    agents_dir = tmp_path / "agents"
    dir_path = _write(agents_dir / "postmortem_lead" / "postmortem_lead.md", "dir\n")
    _write(agents_dir / "postmortem_lead.md", "flat\n")

    assert _resolve_profile_path(agents_dir, "postmortem-lead") == dir_path


@pytest.fixture
def two_roots(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """A project-local agents dir (cwd walk-up) and a user-level one (HOME)."""
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(project)
    return project / ".lionagi" / "agents", tmp_path / ".lionagi" / "agents"


def test_loader_resolves_hyphen_request_in_project_root(two_roots) -> None:
    project_agents, _ = two_roots
    _write(project_agents / "postmortem_lead.md", "---\nmodel: project\n---\n\nbody\n")

    assert load_agent_profile("postmortem-lead").model == "project"


def test_loader_resolves_hyphen_request_in_user_root(two_roots) -> None:
    _, home_agents = two_roots
    _write(home_agents / "postmortem_lead.md", "---\nmodel: home\n---\n\nbody\n")

    assert load_agent_profile("postmortem-lead").model == "home"


def test_project_root_still_beats_user_root(two_roots) -> None:
    project_agents, home_agents = two_roots
    _write(project_agents / "postmortem_lead.md", "---\nmodel: project\n---\n\nbody\n")
    _write(home_agents / "postmortem_lead.md", "---\nmodel: home\n---\n\nbody\n")

    assert load_agent_profile("postmortem_lead").model == "project"


def test_project_root_beats_user_root_across_spellings(two_roots) -> None:
    """Root order decides; the requested spelling does not promote a lower root."""
    project_agents, home_agents = two_roots
    _write(project_agents / "postmortem_lead.md", "---\nmodel: project\n---\n\nbody\n")
    _write(home_agents / "postmortem-lead.md", "---\nmodel: home\n---\n\nbody\n")

    assert load_agent_profile("postmortem-lead").model == "project"
    assert load_agent_profile("postmortem_lead").model == "project"


def test_loader_prefers_the_exact_spelling_within_one_root(two_roots) -> None:
    """Both spellings present in one root: each request gets the file it named."""
    project_agents, _ = two_roots
    _write(project_agents / "postmortem-lead.md", "---\nmodel: hyphen\n---\n\nbody\n")
    _write(project_agents / "postmortem_lead.md", "---\nmodel: underscore\n---\n\nbody\n")

    assert load_agent_profile("postmortem-lead").model == "hyphen"
    assert load_agent_profile("postmortem_lead").model == "underscore"


def test_loader_reports_ambiguity_when_nothing_matches_exactly(two_roots) -> None:
    project_agents, _ = two_roots
    underscored = _write(project_agents / "postmortem_lead_v2.md", "---\nmodel: a\n---\n\nbody\n")
    hyphenated = _write(project_agents / "postmortem-lead-v2.md", "---\nmodel: b\n---\n\nbody\n")

    with pytest.raises(AmbiguousProfileNameError) as excinfo:
        load_agent_profile("postmortem-lead_v2")
    message = str(excinfo.value)
    assert "postmortem-lead_v2" in message
    assert str(underscored) in message
    assert str(hyphenated) in message
