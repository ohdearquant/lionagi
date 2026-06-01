# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""lionagi.orchestration — multi-agent orchestration over lionagi primitives.

Built on what lionagi already has, not a parallel stack:

- **Roles** are casts ``AgentSpec``/``Profile`` wired into a ``Session`` as
  branches (``spawn_roles``).
- **Plans** are ``casts.emission`` types — ``ExecutionPlan`` + ``TaskAssignment``
  (what an orchestrator role already emits).
- **Execution** is ``session.flow``. With ``reactive=True`` the DAG is
  self-expanding: a running agent emits a ``SpawnRequest`` and a new operation
  is injected into the live graph — no halt, no re-plan-and-rerun.

Quick start::

    from lionagi import Session
    from lionagi.orchestration import spawn_roles, fanout
    from lionagi.casts.emission import TaskAssignment

    session = Session()
    roles = await spawn_roles(
        session,
        {"researcher": "researcher", "architect": "architect",
         "synthesizer": "synthesizer"},
        spawners=["architect"],          # may grow the DAG
    )
    result = await fanout(
        session,
        [TaskAssignment(task="survey prior art", assignee="researcher"),
         TaskAssignment(task="draft the design", assignee="architect")],
        roles,
        synthesis_role="synthesizer",
        reactive=True,
    )
"""

from __future__ import annotations

from .patterns import (
    build_dag_graph,
    build_fanout_graph,
    fanout,
    grant_spawn,
    plan,
    role_node_builder,
    spawn_roles,
)

__all__ = (
    "spawn_roles",
    "plan",
    "fanout",
    "build_fanout_graph",
    "build_dag_graph",
    "role_node_builder",
    "grant_spawn",
)
