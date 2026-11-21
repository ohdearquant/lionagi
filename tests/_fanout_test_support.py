# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Fanout run setup shared by the hub's orchestration tests and the Studio application's tests."""

from __future__ import annotations

import asyncio

from lionagi import Branch, Session
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.types import EventStatus
from lionagi.session.signal import NodeCompleted, NodeFailed
from lionhub.orchestration.runs import RunDir
from lionhub.serve.cli.orchestrate._orchestration import OrchestrationEnv


def _fanout_env(tmp_path) -> tuple[OrchestrationEnv, RunDir, Session]:
    orchestrator = Branch(name="orchestrator")
    session = Session(default_branch=orchestrator)
    run = RunDir(
        run_id="fanout-run",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    run.ensure_artifact_root()
    env = OrchestrationEnv(
        run=run,
        session=session,
        orc_branch=orchestrator,
        builder=OperationGraphBuilder(),
        orc_profile=None,
        orc_profile_name=None,
        default_model_spec="codex/model",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )
    return env, run, session


def _completed(session, node, response, name="worker"):
    """Leave a node the way a real run_dag leaves a finished one — terminal status
    included, so a later pass over the same graph does not re-execute it."""
    node.execution.status = EventStatus.COMPLETED
    node.execution.response = response
    return asyncio.create_task(
        session.emit(NodeCompleted(op_id=str(node.id), name=name, elapsed=0.01))
    )


def _failed(session, node, name="worker"):
    node.execution.status = EventStatus.FAILED
    return asyncio.create_task(
        session.emit(NodeFailed(op_id=str(node.id), name=name, elapsed=0.01))
    )
