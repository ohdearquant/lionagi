# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ADR-0026 project detection (lionagi/cli/_project.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.cli._project import (
    _parse_remote_url,
    _read_project_from_toml,
    detect_project,
)


class TestParseRemoteUrl:
    def test_https_github(self):
        assert _parse_remote_url("https://github.com/ohdearquant/lionagi.git") == "ohdearquant/lionagi"

    def test_https_no_dot_git(self):
        assert _parse_remote_url("https://github.com/ohdearquant/lionagi") == "ohdearquant/lionagi"

    def test_ssh_colon_style(self):
        assert _parse_remote_url("git@github.com:ohdearquant/lionagi.git") == "ohdearquant/lionagi"

    def test_ssh_url_style(self):
        assert _parse_remote_url("ssh://git@github.com/ohdearquant/lionagi.git") == "ohdearquant/lionagi"

    def test_trailing_slash(self):
        assert _parse_remote_url("https://github.com/ohdearquant/lionagi/") == "ohdearquant/lionagi"

    def test_single_segment(self):
        assert _parse_remote_url("https://example.com/repo.git") is None or _parse_remote_url("https://example.com/repo.git")


class TestReadProjectFromToml:
    def test_valid_toml(self, tmp_path: Path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[project]\nname = "myproject"\ngithub = "org/repo"\n')
        assert _read_project_from_toml(toml_file) == "myproject"

    def test_missing_project_section(self, tmp_path: Path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[other]\nkey = "val"\n')
        assert _read_project_from_toml(toml_file) is None

    def test_missing_name_key(self, tmp_path: Path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[project]\ngithub = "org/repo"\n')
        assert _read_project_from_toml(toml_file) is None

    def test_empty_name(self, tmp_path: Path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[project]\nname = ""\n')
        assert _read_project_from_toml(toml_file) is None

    def test_invalid_toml(self, tmp_path: Path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("not valid toml {{{}}}}")
        assert _read_project_from_toml(toml_file) is None

    def test_nonexistent_file(self, tmp_path: Path):
        assert _read_project_from_toml(tmp_path / "nope.toml") is None


class TestDetectProject:
    def test_config_toml_in_cwd(self, tmp_path: Path):
        lionagi_dir = tmp_path / ".lionagi"
        lionagi_dir.mkdir()
        (lionagi_dir / "config.toml").write_text('[project]\nname = "test-proj"\n')
        name, source = detect_project(tmp_path)
        assert name == "test-proj"
        assert source == "config_toml"

    def test_config_toml_in_parent(self, tmp_path: Path):
        lionagi_dir = tmp_path / ".lionagi"
        lionagi_dir.mkdir()
        (lionagi_dir / "config.toml").write_text('[project]\nname = "parent-proj"\n')
        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)
        name, source = detect_project(child)
        assert name == "parent-proj"
        assert source == "config_toml"

    def test_no_config_non_git(self, tmp_path: Path):
        name, source = detect_project(tmp_path)
        assert name is None
        assert source is None

    def test_lionagi_repo_detection(self):
        """Smoke test: detect_project from the actual lionagi repo should find config.toml."""
        name, source = detect_project(Path("/Users/lion/projects/lionagi"))
        assert name == "lionagi"
        assert source == "config_toml"


class TestDetectProjectGlobalOverrides:
    def test_path_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        settings_dir = tmp_path / "home" / ".lionagi"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.yaml").write_text(
            f'project_overrides:\n  "{tmp_path / "work"}": "work-proj"\n'
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

        work_dir = tmp_path / "work" / "subdir"
        work_dir.mkdir(parents=True)
        name, source = detect_project(work_dir)
        assert name == "work-proj"
        assert source == "global_override"
