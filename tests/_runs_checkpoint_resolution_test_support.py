# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._id_resolution_test_support import (
    db_path as db_path,  # noqa: F401
)


@pytest.fixture
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr("lionhub.orchestration.runs.RUNS_ROOT", root)
    monkeypatch.setattr("lionhub.orchestration.runs._LEGACY_AGENTS_ROOT", tmp_path / "absent")
    return root
