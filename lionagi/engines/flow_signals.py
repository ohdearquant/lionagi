# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Persist per-node lifecycle signals for a ``Session.flow`` run.

``DependencyAwareExecutor`` reports queued/started/completed/failed node
transitions through an ``on_progress`` callback and announces reactive spawns as
``NodeSpawned`` bus events. This module turns those into ``NodeQueued`` /
``NodeStarted`` / ``NodeCompleted`` / ``NodeFailed`` signals on the session bus so
a run's DAG can be rendered moving through its lifecycle.

Shared by the engine's own DAG run and by Studio's ``workflow_run`` — both drive
``session.flow`` directly and need the same node-progress signals.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from lionagi.ln.concurrency import gather
from lionagi.session.signal import (
    NodeCompleted,
    NodeFailed,
    NodeQueued,
    NodeSpawned,
    NodeStarted,
)

__all__ = ("flow_progress_signals",)


def _build_node_edge_meta(graph: Any) -> dict[str, dict]:
    """Map each Operation node's id to its {parent_id, depends_on, name} from the graph.

    ``name`` is the node's authored id (``metadata['reference_id']``) when set —
    e.g. the Studio designer box id ``chat1`` — or None. It is preferred over the
    executor-supplied callback name so every lifecycle signal (not just queued)
    maps back to the authored DAG; the executor names started/completed/failed
    after the branch, which need not match the authored id.
    """
    from lionagi.operations.node import Operation

    meta: dict[str, dict] = {}
    for node in graph.internal_nodes.values():
        if not isinstance(node, Operation):
            continue
        preds = [str(e.head) for e in graph.internal_edges.values() if str(e.tail) == str(node.id)]
        meta[str(node.id)] = {
            "parent_id": preds[0] if len(preds) == 1 else None,
            "depends_on": preds,
            "name": node.metadata.get("reference_id"),
        }
    return meta


@contextlib.asynccontextmanager
async def flow_progress_signals(
    session: Any, graph: Any
) -> AsyncIterator[Callable[[str, str, str, float], None]]:
    """Yield an ``on_progress`` callback that persists node-lifecycle signals.

    Usage::

        async with flow_progress_signals(session, graph) as on_progress:
            await session.flow(graph, on_progress=on_progress, ...)

    Tracks reactive spawns (``NodeSpawned``) so late-added nodes carry their
    parent/depends_on edges, and on exit awaits every emitted signal so the
    observers finish persisting before the caller reads what they wrote.
    """
    emits: list[asyncio.Future] = []
    node_edge_meta = _build_node_edge_meta(graph)

    def _on_progress(op_id: str, name: str, status: str, elapsed: float) -> None:
        meta = node_edge_meta.get(op_id) or {}
        parent_id = meta.get("parent_id")
        depends_on = meta.get("depends_on", [])
        # Prefer the authored node id so every lifecycle signal maps back to the
        # designer DAG; fall back to the executor's callback name for nodes with
        # no reference_id (e.g. an engine's own ops, or reactive spawns).
        sig_name = meta.get("name") or name
        if status == "queued":
            sig: Any = NodeQueued(
                op_id=op_id, name=sig_name, parent_id=parent_id, depends_on=depends_on
            )
        elif status == "started":
            sig = NodeStarted(
                op_id=op_id, name=sig_name, parent_id=parent_id, depends_on=depends_on
            )
        elif status == "completed":
            sig = NodeCompleted(
                op_id=op_id,
                name=sig_name,
                elapsed=elapsed,
                parent_id=parent_id,
                depends_on=depends_on,
            )
        elif status == "failed":
            sig = NodeFailed(
                op_id=op_id,
                name=sig_name,
                elapsed=elapsed,
                parent_id=parent_id,
                depends_on=depends_on,
            )
        else:
            return
        # on_progress is sync (called from inside the executor); fan the signal
        # onto the async bus. Collected so the caller can await the observers
        # before reading state they populate. suppress guards the
        # no-running-loop case (nothing would observe anyway).
        with contextlib.suppress(RuntimeError):
            emits.append(asyncio.ensure_future(session.emit(sig)))

    # Keep node_edge_meta current as reactive spawns add nodes after start.
    def _on_spawned(sig: Any, _ctx: Any) -> None:
        if sig.op_id and sig.parent_id is not None:
            node_edge_meta[sig.op_id] = {
                "parent_id": sig.parent_id,
                "depends_on": [sig.parent_id],
            }
        elif sig.op_id:
            node_edge_meta.setdefault(sig.op_id, {"parent_id": None, "depends_on": []})

    session.observe(NodeSpawned, handler=_on_spawned)
    try:
        yield _on_progress
    finally:
        session.observer.unobserve(_on_spawned)
        if emits:
            await gather(*emits, return_exceptions=True)
