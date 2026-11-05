# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test-root consumers read the same project configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _project(tmp_path: Path, roots: list[str]) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in roots:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = " + json.dumps(roots) + "\n",
        encoding="utf-8",
    )
    return root


def test_roots_follow_config_even_when_called_outside_project(tmp_path, monkeypatch):
    from scripts.test_roots import configured_test_roots

    project = _project(tmp_path, ["tests", "lionhub/tests", "extra/tests"])
    monkeypatch.chdir(tmp_path)
    assert configured_test_roots(project) == ("tests", "lionhub/tests", "extra/tests")


@pytest.mark.parametrize(
    "bad", [[], [""], ["."], ["../outside"], ["/absolute"], ["bad\nname"], ["missing"]]
)
def test_roots_refuse_invalid_or_missing_paths(tmp_path, bad):
    from scripts.test_roots import TestRootConfigError, configured_test_roots

    project = _project(tmp_path, [])
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = " + json.dumps(bad) + "\n", encoding="utf-8"
    )
    with pytest.raises(TestRootConfigError):
        configured_test_roots(project)


def test_quarantine_uses_configured_roots_and_rejects_prefix_traversal(tmp_path, monkeypatch):
    from scripts import quarantine

    project = _project(tmp_path, ["tests", "lionhub/tests", "extra/tests"])
    monkeypatch.setattr(quarantine, "REPO_ROOT", project)
    manifest = tmp_path / "quarantine.txt"
    for path in ("tests/test_a.py", "lionhub/tests/test_b.py", "extra/tests/test_c.py"):
        manifest.write_text(f"2026-07-01 | {path}::test_one | example\n", encoding="utf-8")
        assert quarantine.load_manifest(manifest)[0].nodeid == f"{path}::test_one"
    for path in ("tests-other/test.py", "extra/tests-other/test.py", "extra/tests/../outside.py"):
        manifest.write_text(f"2026-07-01 | {path}::test_one | example\n", encoding="utf-8")
        with pytest.raises(quarantine.QuarantineError):
            quarantine.load_manifest(manifest)


def test_quarantine_accepts_sparse_checkout_for_explicit_test(tmp_path, monkeypatch):
    from scripts import quarantine
    from scripts.test_roots import TestRootConfigError, configured_test_roots

    project = _project(tmp_path, ["tests"])
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "lionhub/tests"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(quarantine, "REPO_ROOT", project)
    manifest = tmp_path / "quarantine.txt"
    manifest.write_text("2026-07-01 | tests/test_a.py::test_one | example\n", encoding="utf-8")
    assert quarantine.load_manifest(manifest)[0].nodeid == "tests/test_a.py::test_one"
    with pytest.raises(TestRootConfigError):
        configured_test_roots(project)


def test_absolute_and_relative_configured_targets_keep_junit_node_identities(tmp_path, monkeypatch):
    from scripts.check_suite_coverage import collected_nodeids

    project = _project(tmp_path, ["tests", "hub/tests"])
    (project / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (project / "hub" / "tests" / "test_b.py").write_text("def test_b(): pass\n", encoding="utf-8")
    monkeypatch.chdir(project)
    relative = collected_nodeids("", ["tests", "hub/tests"])
    monkeypatch.chdir(tmp_path)
    absolute = collected_nodeids("", [str(project / "tests"), str(project / "hub" / "tests")])
    assert relative == absolute
    assert {file for file, _, _ in relative} == {"tests/test_a.py", "hub/tests/test_b.py"}


def test_roots_refuse_missing_and_malformed_config(tmp_path):
    from scripts.test_roots import TestRootConfigError, configured_test_roots

    with pytest.raises(TestRootConfigError):
        configured_test_roots(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest\n", encoding="utf-8")
    with pytest.raises(TestRootConfigError):
        configured_test_roots(tmp_path)


def test_quarantine_refuses_if_config_cannot_be_read(tmp_path, monkeypatch):
    from scripts import quarantine

    monkeypatch.setattr(quarantine, "REPO_ROOT", tmp_path)
    manifest = tmp_path / "quarantine.txt"
    manifest.write_text("", encoding="utf-8")
    with pytest.raises(quarantine.QuarantineError, match="test roots"):
        quarantine.load_manifest(manifest)
