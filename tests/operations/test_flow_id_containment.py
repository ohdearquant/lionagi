# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Security regression tests for artifact-dir path containment.

PR review 2026-04-24 flagged a HIGH finding: agent ids become filesystem path
segments via ``RunDir.agent_artifact_dir``. After the orchestrate clean break,
agent ids are CLI-generated from roster-validated role names (``{assignee}-{i}``)
rather than free-form model output — but ``RunDir.agent_artifact_dir`` remains
the authoritative, defense-in-depth guard and is what these tests pin: it
rejects components containing path separators, leading dots, or resolved paths
outside the artifact root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.cli._runs import RunDir

# ── RunDir.agent_artifact_dir containment ───────────────────────────


class TestAgentArtifactDirContainment:
    @pytest.fixture
    def rundir(self, tmp_path: Path) -> RunDir:
        # Minimal RunDir — only artifact_root is used by agent_artifact_dir
        artifact_root = tmp_path / "runs" / "r1" / "artifacts"
        artifact_root.mkdir(parents=True)
        state_root = tmp_path / "runs" / "r1"
        return RunDir(
            run_id="r1",
            state_root=state_root,
            artifact_root=artifact_root,
        )

    def test_safe_id_resolves(self, rundir: RunDir):
        path = rundir.agent_artifact_dir("impl1")
        assert path == rundir.artifact_root / "impl1"

    def test_rejects_traversal(self, rundir: RunDir):
        with pytest.raises(ValueError, match="safe path component"):
            rundir.agent_artifact_dir("../evil")

    def test_rejects_absolute(self, rundir: RunDir):
        with pytest.raises(ValueError, match="safe path component"):
            rundir.agent_artifact_dir("/etc/passwd")

    def test_rejects_backslash(self, rundir: RunDir):
        with pytest.raises(ValueError, match="safe path component"):
            rundir.agent_artifact_dir("a\\b")

    def test_rejects_empty(self, rundir: RunDir):
        with pytest.raises(ValueError, match="safe path component"):
            rundir.agent_artifact_dir("")

    def test_rejects_dot(self, rundir: RunDir):
        with pytest.raises(ValueError, match="safe path component"):
            rundir.agent_artifact_dir(".")

    def test_rejects_non_string(self, rundir: RunDir):
        with pytest.raises(ValueError, match="safe path component"):
            rundir.agent_artifact_dir(None)  # type: ignore[arg-type]
