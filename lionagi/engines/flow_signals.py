# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Turn executor node transitions into NodeQueued/Started/Completed/Failed
session-bus signals for a live-rendered Session.flow DAG run (shared by the engine and Studio)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from lionagi.ln.concurrency import gather
from lionagi.session.signal import (
    NodeCancelled,
    NodeCompleted,
    NodeFailed,
    NodeQueued,
    NodeSkipped,
    NodeSpawned,
    NodeStarted,
)

__all__ = ("flow_progress_signals",)


def _build_node_edge_meta(graph: Any) -> dict[str, dict]:
    """Map each Operation node id to {parent_id, depends_on, name}; name prefers the
    authored reference_id over the executor's callback name (renamed post-hoc)."""
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
    session: Any, graph: Any, *, skip_ops: set[Any] | None = None
) -> AsyncIterator[Callable[[str, str, str, float, bool], None]]:
    """Yield an ``on_progress`` callback that persists node-lifecycle signals; awaits
    every emitted signal on exit so observers finish before the caller reads what they wrote.

    ``skip_ops`` names ops that already ran in an earlier pass. A pass over a graph
    whose nodes already ran reports those nodes again, and signalling them a second
    time writes terminal events for work this pass did not do; a resume rebuilt from
    those events treats the replay as completed. Naming what to *skip* rather than
    what to signal is what keeps a node created during the pass audible: a spawn is
    absent from the set because it did not exist when the caller named it, and its
    first signal is emitted in the same synchronous admission call as the spawn
    notification, so there is no moment at which a caller could have added it.
    """
    emits: list[asyncio.Future] = []
    node_edge_meta = _build_node_edge_meta(graph)
    # Normalised to str: node ids travel as UUID in the builder's return values and
    # as str on the signals, so an un-normalised set matches nothing and silently
    # replays every node it was given to suppress.
    skipped: set[str] = set() if skip_ops is None else {str(op) for op in skip_ops}

    def _on_progress(
        op_id: str,
        name: str,
        status: str,
        elapsed: float,
        name_is_fallback: bool,
    ) -> None:
        if op_id in skipped:
            return
        meta = node_edge_meta.setdefault(op_id, {})
        parent_id = meta.get("parent_id")
        depends_on = meta.get("depends_on", [])
        # Pin the first genuinely-resolved name per op_id (see
        # docs/internals/core.md, engines/flow_signals.py) so later signals
        # stay correlated even if the branch is later renamed.
        sig_name = meta.get("name")
        if sig_name is None:
            sig_name = name
            if not name_is_fallback:
                meta["name"] = sig_name
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
        elif status == "skipped":
            sig = NodeSkipped(
                op_id=op_id,
                name=sig_name,
                elapsed=elapsed,
                parent_id=parent_id,
                depends_on=depends_on,
            )
        elif status == "cancelled":
            sig = NodeCancelled(
                op_id=op_id,
                name=sig_name,
                elapsed=elapsed,
                parent_id=parent_id,
                depends_on=depends_on,
            )
        else:
            return
        # on_progress is sync; fan the signal onto the async bus, collected so the
        # caller can await observers before reading what they wrote.
        with contextlib.suppress(RuntimeError):
            emits.append(asyncio.ensure_future(session.emit(sig)))

    # Keep node_edge_meta current as reactive spawns add nodes after start.
    # Updates the entry in place rather than replacing it -- op_id was already
    # queued (in the same synchronous admission call that emits this signal),
    # which may have already pinned "name"; a wholesale replacement here would
    # drop it and reopen the started/terminal name-split this guards against.
    def _on_spawned(sig: Any, _ctx: Any) -> None:
        if not sig.op_id:
            return
        entry = node_edge_meta.setdefault(sig.op_id, {"parent_id": None, "depends_on": []})
        if sig.parent_id is not None:
            entry["parent_id"] = sig.parent_id
            entry["depends_on"] = [sig.parent_id]

    session.observe(NodeSpawned, handler=_on_spawned)
    try:
        yield _on_progress
    finally:
        session.observer.unobserve(_on_spawned)
        if emits:
            await gather(*emits, return_exceptions=True)
