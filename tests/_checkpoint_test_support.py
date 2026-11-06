# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lionagi import Branch, Session
from lionagi.cli.orchestrate._orchestration import OrchestrationEnv


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _minimal_real_env() -> OrchestrationEnv:
    """A real Session/Branch env (no provider setup) for start/stop_live_persist tests."""
    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    return OrchestrationEnv(
        run=MagicMock(run_id="persist-test"),
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        orc_profile_name=None,
        default_model_spec="claude",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )
