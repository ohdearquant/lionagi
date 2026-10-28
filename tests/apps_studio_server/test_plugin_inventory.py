# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A plugin's detail scans its skills and agents once, and its summary fields
match the summary route's for the same directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")


def _plugin(tmp_path: Path) -> Path:
    plugin_dir = tmp_path / "repo" / "marketplace" / "inv"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "inv", "description": "Inventory plugin", "version": "1.2.3"})
    )
    for skill in ("alpha", "beta"):
        (plugin_dir / "skills" / skill).mkdir(parents=True)
        (plugin_dir / "skills" / skill / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: {skill} skill\n---\nbody\n"
        )
    (plugin_dir / "agents").mkdir()
    (plugin_dir / "agents" / "reader.md").write_text("---\ndescription: reads\n---\nprompt\n")
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / "hooks" / "hooks.json").write_text("{}")
    return plugin_dir


@pytest.mark.parametrize("redact", [False, True])
def test_plugin_detail_scans_once_and_agrees_with_the_summary(tmp_path, monkeypatch, redact):
    import lionagi.studio.services.plugins as plugins_mod

    plugin_dir = _plugin(tmp_path)
    monkeypatch.setattr(plugins_mod, "_REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(plugins_mod, "MARKETPLACE_DIR", tmp_path / "repo" / "marketplace")

    scans: list[str] = []
    real_skills, real_agents = plugins_mod._scan_skills, plugins_mod._scan_agents

    def _skills(d):
        scans.append("skills")
        return real_skills(d)

    def _agents(d):
        scans.append("agents")
        return real_agents(d)

    monkeypatch.setattr(plugins_mod, "_scan_skills", _skills)
    monkeypatch.setattr(plugins_mod, "_scan_agents", _agents)

    detail = plugins_mod._plugin_detail(plugin_dir, "inv", "d", "marketplace", redact=redact)
    assert sorted(scans) == ["agents", "skills"]

    summary = plugins_mod._plugin_summary(plugin_dir, "inv", "d", "marketplace", redact=redact)
    assert {key: detail[key] for key in summary} == summary
    assert summary["skill_count"] == len(detail["skills"]) == 2
    assert summary["agent_count"] == len(detail["agents"]) == 1
    assert summary["has_hooks"] is True
    assert summary["version"] == "1.2.3"
