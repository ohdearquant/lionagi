# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest
import yaml

from lionagi.config_resolution import ResourceKind, resolve_config


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=True))


def _project_root(base: Path, name: str = "project") -> Path:
    root = base / name / ".lionagi"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _home_root(base: Path) -> Path:
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".lionagi").mkdir(parents=True, exist_ok=True)
    return home


def test_resolve_config_cascade_cli_env_project_user(tmp_path, monkeypatch):
    home = _home_root(tmp_path)
    project = _project_root(tmp_path, "project")

    monkeypatch.setenv("HOME", str(home))

    _write_yaml(
        home / ".lionagi" / "agents" / "researcher.yaml",
        {
            "level": "user",
            "shared": {"source": "user", "mode": "debug"},
            "items": [1, 2, 3],
            "nested": {"keep": "user"},
        },
    )
    _write_yaml(
        project / "agents" / "researcher.yaml",
        {
            "level": "project",
            "shared": {"source": "project", "mode": "fast"},
            "items": [7],
            "nested": {"keep": "project", "override": "project"},
        },
    )
    monkeypatch.setenv(
        "LIONAGI_AGENT_RESEARCHER",
        yaml.safe_dump(
            {
                "shared": {"mode": "env", "timeout": 25},
                "items": [9],
            }
        ),
    )
    monkeypatch.setenv(
        "LIONAGI_CLI_AGENT_RESEARCHER",
        yaml.safe_dump(
            {
                "shared": {"source": "cli", "timeout": 10},
                "nested": {"override": "cli"},
            }
        ),
    )

    cfg = resolve_config(ResourceKind.AGENT, "researcher", project=str(project.parent))

    assert cfg["level"] == "project"
    assert cfg["shared"]["source"] == "cli"
    assert cfg["shared"]["mode"] == "env"
    assert cfg["shared"]["timeout"] == 10
    assert cfg["nested"] == {"keep": "project", "override": "cli"}
    assert cfg["items"] == [9]

    prov = cfg["_provenance"]
    assert prov["sources"]["user"].endswith(str(home / ".lionagi" / "agents" / "researcher.yaml"))
    assert prov["sources"]["project"].endswith(str(project / "agents" / "researcher.yaml"))
    assert prov["sources"]["env"] == "LIONAGI_AGENT_RESEARCHER"
    assert prov["sources"]["cli"] == "LIONAGI_CLI_AGENT_RESEARCHER"
    assert prov["keys"]["level"] == "project"
    assert prov["keys"]["shared.mode"] == "env"
    assert prov["keys"]["shared.timeout"] == "cli"
    assert prov["keys"]["nested.override"] == "cli"
    assert prov["keys"]["nested.keep"] == "project"
    assert prov["keys"]["items"] == "env"


def test_resolve_config_deep_merge_not_replace_lists(tmp_path, monkeypatch):
    home = _home_root(tmp_path)
    project = _project_root(tmp_path, "project")
    monkeypatch.setenv("HOME", str(home))

    _write_yaml(
        home / ".lionagi" / "agents" / "pipeline.yaml",
        {
            "runtime": {"retries": 1, "backend": "user"},
            "items": ["user-a", "user-b"],
            "opts": {"parallel": True},
        },
    )
    _write_yaml(
        project / "agents" / "pipeline.yaml",
        {
            "runtime": {"retries": 2, "debug": False},
            "items": ["project-a"],
            "opts": {"timeout": 30},
        },
    )
    monkeypatch.setenv(
        "LIONAGI_AGENT_PIPELINE",
        yaml.safe_dump(
            {
                "runtime": {"parallel": 4, "debug": True},
                "items": ["env-only"],
            }
        ),
    )

    cfg = resolve_config(ResourceKind.AGENT, "pipeline", project=str(project.parent))

    assert cfg["runtime"] == {
        "retries": 2,
        "backend": "user",
        "debug": True,
        "parallel": 4,
    }
    assert cfg["items"] == ["env-only"]
    assert cfg["opts"] == {"parallel": True, "timeout": 30}

    prov = cfg["_provenance"]["keys"]
    assert prov["runtime.retries"] == "project"
    assert prov["runtime.backend"] == "user"
    assert prov["runtime.parallel"] == "env"
    assert prov["runtime.debug"] == "env"
    assert prov["items"] == "env"
    assert prov["opts.timeout"] == "project"
    assert prov["opts.parallel"] == "user"


def test_project_level_overrides_user_level(tmp_path, monkeypatch):
    home = _home_root(tmp_path)
    project = _project_root(tmp_path, "project")
    monkeypatch.setenv("HOME", str(home))

    _write_yaml(
        home / ".lionagi" / "agents" / "runner.yaml",
        {
            "value": "from_user",
            "only_user": True,
        },
    )
    _write_yaml(
        project / "agents" / "runner.yaml",
        {
            "value": "from_project",
            "only_project": True,
        },
    )

    cfg = resolve_config(ResourceKind.AGENT, "runner", project=str(project.parent))
    assert cfg["value"] == "from_project"
    assert cfg["only_user"] is True
    assert cfg["only_project"] is True
    assert cfg["_provenance"]["keys"]["value"] == "project"


def test_resolve_config_missing_config_returns_defaults(tmp_path, monkeypatch):
    home = _home_root(tmp_path)
    project = _project_root(tmp_path, "project")
    monkeypatch.setenv("HOME", str(home))

    cfg = resolve_config(ResourceKind.SKILL, "missing", project=str(project.parent))

    assert set(cfg.keys()) == {"_provenance"}
    assert cfg["_provenance"]["sources"]["default"] == "builtin"
    assert cfg["_provenance"]["sources"]["user"] is None
    assert cfg["_provenance"]["sources"]["project"] is None
    assert cfg["_provenance"]["sources"]["env"] is None
    assert cfg["_provenance"]["sources"]["cli"] is None


@pytest.mark.parametrize("kind", list(ResourceKind))
def test_resolve_config_covers_all_resource_kinds(kind, tmp_path, monkeypatch):
    home = _home_root(tmp_path)
    project = _project_root(tmp_path, "project")
    monkeypatch.setenv("HOME", str(home))

    cfg = resolve_config(kind, "unit", project=str(project.parent))
    assert "_provenance" in cfg
    assert cfg["_provenance"]["sources"]["default"] == "builtin"
    assert isinstance(cfg["_provenance"]["keys"], dict)

    if kind is not ResourceKind.SETTINGS:
        assert isinstance(cfg, dict)
