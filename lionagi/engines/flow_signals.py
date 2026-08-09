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
    session: Any, graph: Any
) -> AsyncIterator[Callable[[str, str, str, float, bool], None]]:
    """Yield an ``on_progress`` callback that persists node-lifecycle signals; awaits
    every emitted signal on exit so observers finish before the caller reads what they wrote."""
    emits: list[asyncio.Future] = []
    node_edge_meta = _build_node_edge_meta(graph)

    def _on_progress(
        op_id: str,
        name: str,
        status: str,
        elapsed: float,
        name_is_fallback: bool,
    ) -> None:
        meta = node_edge_meta.setdefault(op_id, {})
        parent_id = meta.get("parent_id")
        depends_on = meta.get("depends_on", [])
        # Prefer the authored node id so every lifecycle signal maps back to the
        # designer DAG; fall back to the executor's name (engine's own ops, reactive spawns).
        # Pin the first GENUINELY resolved name for this op_id so later
        # started/completed/failed calls reuse it even if a branch-naming hook
        # (spawn_branch_setup) later renames the operation's cloned branch --
        # the branch name is a display concern, not the correlation key.
        # Whether a name is a placeholder ("no reference_id / no branch bound
        # yet", see flow.py's _display_name/_branch_display_name) is decided
        # structurally by the producer and passed in via name_is_fallback --
        # never inferred here by comparing against op_id's prefix, since a
        # genuine authored name can coincide with that prefix by chance.
        #
        # name_is_fallback has no default: this is an internal seam with an
        # enumerable, all-internal caller set (the four lifecycle producers in
        # operations/flow.py, all of which already pass the bit explicitly).
        # "Unknown provenance" isn't a real state a caller can be in here, so
        # there is no safe value to default to -- treating an untagged call as
        # fallback silently produced the exact split-identity bug this guards
        # against for a caller whose first name happened to be authored.
        # Requiring the keyword makes a caller that doesn't know its own
        # provenance fail loudly (TypeError) instead of guessing wrong.
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
