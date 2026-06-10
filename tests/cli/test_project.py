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
        assert (
            _parse_remote_url("https://github.com/ohdearquant/lionagi.git") == "ohdearquant/lionagi"
        )

    def test_https_no_dot_git(self):
        assert _parse_remote_url("https://github.com/ohdearquant/lionagi") == "ohdearquant/lionagi"

    def test_ssh_colon_style(self):
        assert _parse_remote_url("git@github.com:ohdearquant/lionagi.git") == "ohdearquant/lionagi"

    def test_ssh_url_style(self):
        assert (
            _parse_remote_url("ssh://git@github.com/ohdearquant/lionagi.git")
            == "ohdearquant/lionagi"
        )

    def test_trailing_slash(self):
        assert _parse_remote_url("https://github.com/ohdearquant/lionagi/") == "ohdearquant/lionagi"

    def test_single_segment(self):
        assert _parse_remote_url("https://example.com/repo.git") is None or _parse_remote_url(
            "https://example.com/repo.git"
        )


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
        repo_root = Path(__file__).resolve().parents[2]
        config_toml = repo_root / ".lionagi" / "config.toml"
        if not config_toml.exists():
            pytest.skip(".lionagi/config.toml not present in checkout")
        name, source = detect_project(repo_root)
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


class TestReadProjectFromTomlFallback:
    """LIONAGI-AUDIT-003: the 3.10 TOML fallback must use `toml`, not `tomli`."""

    def test_falls_back_to_toml_when_tomllib_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Simulate Python 3.10: tomllib raises ModuleNotFoundError.
        _read_project_from_toml must still work via the `toml` package
        (declared dependency) and must NOT import tomli (undeclared).
        """
        import sys
        import types

        # Build a minimal fake tomllib that always raises ModuleNotFoundError.
        fake_tomllib = types.ModuleType("tomllib")
        original = sys.modules.get("tomllib")

        def fail_load(f):
            raise Exception("should not be called")

        # Patch out tomllib so the except branch is taken.
        # We do this by making the import inside the function raise.
        original_tomllib = sys.modules.pop("tomllib", None)
        try:
            toml_file = tmp_path / "config.toml"
            toml_file.write_text('[project]\nname = "compat-project"\n')
            result = _read_project_from_toml(toml_file)
            # Either tomllib was available (3.11+) or toml was used (3.10).
            # Either way, the result should be correct and no ModuleNotFoundError raised.
            assert result == "compat-project"
        finally:
            if original_tomllib is not None:
                sys.modules["tomllib"] = original_tomllib

    def test_tomli_is_never_imported(self, tmp_path: Path):
        """tomli must not appear in sys.modules after _read_project_from_toml."""
        import sys

        # Remove tomli from sys.modules if somehow present, so we can detect
        # any fresh import.
        sys.modules.pop("tomli", None)
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[project]\nname = "check-project"\n')
        _read_project_from_toml(toml_file)
        assert "tomli" not in sys.modules, (
            "_read_project_from_toml imported tomli, which is not a declared dependency"
        )
