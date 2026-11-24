# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A written graph of agent legs that a flow runs with no planner (ADR-0069 Amendment 4).

The file is UTF-8 JSON with one top-level key, ``nodes``: a list of objects with ``id``,
``agent``, ``prompt``, ``needs`` and, optionally, ``exit_criteria``. :func:`load_graph`
reads it once under ADR-0048 Amendment 1 H2 and checks its shape; :func:`check_graph`
applies the refusals of G3 that need no running flow; :func:`write_graph_copy` writes the
accepted bytes into a run's own directory, which is the only copy a run reads.

``flow.submit`` and ``li o flow --graph`` both call these functions, so one file gets one
answer on both paths.
"""

from __future__ import annotations

import errno
import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lionhub.orchestration.json_file import read_json_file

__all__ = (
    "GRAPH_COPY_FILENAME",
    "MAX_GRAPH_BYTES",
    "MAX_GRAPH_NODES",
    "FlowGraphError",
    "GraphNode",
    "GraphSource",
    "check_graph",
    "graph_flag_refusal",
    "load_graph",
    "load_graph_record",
    "write_graph_copy",
)

MAX_GRAPH_BYTES = 256 * 1024
# The planner path truncates a plan at this many assignments; a written graph is refused
# above it instead, since truncating a graph would drop nodes others may need.
MAX_GRAPH_NODES = 200
GRAPH_COPY_FILENAME = "graph.json"

_NODE_KEYS = frozenset({"id", "agent", "prompt", "needs", "exit_criteria"})
_REQUIRED_NODE_KEYS = ("id", "agent", "prompt", "needs")
_RECORD_KEYS = frozenset({"sha256", "path", "resolved"})
_HEX = frozenset("0123456789abcdef")


class FlowGraphError(ValueError):
    """A written graph is refused. ``rule`` names the rule it broke."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class GraphNode:
    """One leg of a written graph."""

    id: str
    agent: str
    prompt: str
    needs: tuple[str, ...]
    exit_criteria: str | None = None


@dataclass(frozen=True)
class GraphSource:
    """The accepted bytes of a graph file, where they came from, and the nodes read from them."""

    path: str
    resolved: str
    data: bytes
    sha256: str
    nodes: tuple[GraphNode, ...]

    def record(self) -> dict[str, str]:
        """The ``graph`` field a run's checkpoint config carries."""
        return {"sha256": self.sha256, "path": self.path, "resolved": self.resolved}

    def plan(self) -> tuple[list[dict[str, Any]], list[str], list[list[int]]]:
        """The graph as a flow's plan: assignment fields, agent ids and dependency positions.

        Each node's ``needs`` become the positions of the nodes it names, in the order it
        names them, with a repeated name kept once.
        """
        position = {node.id: i for i, node in enumerate(self.nodes)}
        fields: list[dict[str, Any]] = []
        dep_indices: list[list[int]] = []
        for node in self.nodes:
            fields.append(
                {"task": node.prompt, "assignee": node.agent, "exit_criteria": node.exit_criteria}
            )
            dep_indices.append(list(dict.fromkeys(position[need] for need in node.needs)))
        return fields, [node.id for node in self.nodes], dep_indices


def _label(path: str) -> str:
    return f"graph {path!r}"


def _node_name(node: dict[str, Any], index: int) -> str:
    node_id = node.get("id")
    if isinstance(node_id, str) and node_id:
        return f"node {node_id!r}"
    return f"node at position {index}"


def _nodes_from(document: dict[str, Any], path: str) -> tuple[GraphNode, ...]:
    where = _label(path)
    extra = sorted(set(document) - {"nodes"})
    if extra:
        raise FlowGraphError(
            "schema", f"{where} has top-level key(s) a graph does not take: {', '.join(extra)}"
        )
    if "nodes" not in document:
        raise FlowGraphError("schema", f"{where} has no 'nodes' list")
    raw_nodes = document["nodes"]
    if not isinstance(raw_nodes, list):
        raise FlowGraphError("schema", f"{where}: 'nodes' must be a list")
    nodes: list[GraphNode] = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise FlowGraphError("schema", f"{where}: node at position {index} is not an object")
        name = _node_name(raw, index)
        unknown = sorted(set(raw) - _NODE_KEYS)
        if unknown:
            raise FlowGraphError(
                "schema", f"{where}: {name} has key(s) a node does not take: {', '.join(unknown)}"
            )
        missing = [key for key in _REQUIRED_NODE_KEYS if key not in raw]
        if missing:
            raise FlowGraphError("schema", f"{where}: {name} has no {', '.join(missing)}")
        for key in ("id", "agent", "prompt"):
            value = raw[key]
            if not isinstance(value, str) or not value.strip():
                raise FlowGraphError(
                    "schema", f"{where}: {name} needs '{key}' to be a non-empty string"
                )
        needs = raw["needs"]
        if not isinstance(needs, list) or any(not isinstance(need, str) for need in needs):
            raise FlowGraphError("schema", f"{where}: {name} needs 'needs' to be a list of ids")
        exit_criteria = raw.get("exit_criteria")
        if exit_criteria is not None and not isinstance(exit_criteria, str):
            raise FlowGraphError(
                "schema", f"{where}: {name} needs 'exit_criteria' to be a string when given"
            )
        nodes.append(
            GraphNode(
                id=raw["id"],
                agent=raw["agent"],
                prompt=raw["prompt"],
                needs=tuple(needs),
                exit_criteria=exit_criteria,
            )
        )
    return tuple(nodes)


def load_graph(
    path: Any, *, given_path: str | None = None, resolved: str | None = None
) -> GraphSource:
    """Read the graph at *path* once and check its shape.

    *given_path* and *resolved*, when a run reads a copy written for it at submit, name the
    file the caller gave and where it resolved, so the run records those rather than the
    copy's own path.
    """
    read = read_json_file(path, label="graph", max_bytes=MAX_GRAPH_BYTES, error=FlowGraphError)
    source = GraphSource(
        path=read.path,
        resolved=read.resolved_path,
        data=read.data,
        sha256=read.sha256,
        nodes=_nodes_from(read.document, given_path or read.path),
    )
    if given_path is not None or resolved is not None:
        source = replace(
            source,
            path=given_path if given_path is not None else source.path,
            resolved=resolved if resolved is not None else source.resolved,
        )
    return source


def _cycle(nodes: tuple[GraphNode, ...]) -> list[str] | None:
    """The ids on one cycle, in order, or None when the graph has none."""
    needs = {node.id: node.needs for node in nodes}
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished
    for start in needs:
        if state.get(start):
            continue
        path: list[str] = []
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node_id, next_index = stack.pop()
            if next_index == 0:
                state[node_id] = 1
                path.append(node_id)
            successors = needs[node_id]
            if next_index < len(successors):
                stack.append((node_id, next_index + 1))
                successor = successors[next_index]
                seen = state.get(successor)
                if seen == 1:
                    return [*path[path.index(successor) :], successor]
                if not seen:
                    stack.append((successor, 0))
            else:
                state[node_id] = 2
                path.pop()
    return None


def check_graph(source: GraphSource, *, cwd: str | os.PathLike | None, max_ops: int = 0) -> None:
    """Refuse a graph under ADR-0069 Amendment 4 G3 rules 1 to 8.

    A profile resolves from *cwd*, the directory the run starts in, as ``agent.submit``'s
    does; ``None`` is this process's working directory.
    """
    from lionagi.libs.path_safety import validate_path_component
    from lionhub.orchestration.spawn import (
        AgentProfileNotFoundError,
        AmbiguousProfileNameError,
        load_agent_profile,
    )

    where = _label(source.path)
    nodes = source.nodes
    if not nodes:
        raise FlowGraphError("empty", f"{where} has an empty 'nodes' list")
    if len(nodes) > MAX_GRAPH_NODES:
        raise FlowGraphError(
            "too-many-nodes",
            f"{where} has {len(nodes)} nodes; a graph may have at most {MAX_GRAPH_NODES}, "
            "and a written graph is never truncated",
        )
    if max_ops > 0 and len(nodes) > max_ops:
        raise FlowGraphError(
            "max-ops", f"{where} has {len(nodes)} nodes, more than --max-ops {max_ops}"
        )
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            raise FlowGraphError("repeated-id", f"{where}: node id {node.id!r} is repeated")
        seen.add(node.id)
    for index, node in enumerate(nodes):
        try:
            validate_path_component(node.id, label="id")
        except ValueError:
            raise FlowGraphError(
                "unsafe-id",
                f"{where}: the id of the node at position {index} is not a safe path "
                "component, and each id names that node's artifact directory",
            ) from None
    for node in nodes:
        for need in node.needs:
            if need == node.id:
                raise FlowGraphError("self-need", f"{where}: node {node.id!r} needs itself")
            if need not in seen:
                raise FlowGraphError(
                    "unknown-need",
                    f"{where}: node {node.id!r} needs {need!r}, which names no node",
                )
    cycle = _cycle(nodes)
    if cycle is not None:
        raise FlowGraphError("cycle", f"{where} has a cycle: {' -> '.join(cycle)}")
    profile_cwd = None if cwd is None else Path(cwd).expanduser()
    for node in nodes:
        try:
            load_agent_profile(node.agent, cwd=profile_cwd)
        except (AgentProfileNotFoundError, AmbiguousProfileNameError, ValueError) as exc:
            raise FlowGraphError(
                "unknown-agent",
                f"{where}: node {node.id!r} names agent {node.agent!r}, which resolves to no "
                f"profile: {exc}",
            ) from None


# Flags a written graph cannot be combined with, each with the reason a refusal gives.
_GRAPH_FLAG_REFUSALS = (
    ("playbook", "-p/--playbook", "a playbook's prompt asks the planner for a plan"),
    ("file", "-f/--file", "a spec file's prompt asks the planner for a plan"),
    ("resume", "--resume", "a resume reads its graph from the checkpoint it replays"),
    (
        "with_synthesis",
        "--with-synthesis",
        "a graph's result is each node's output; write a node for a summary",
    ),
    ("bare", "--bare", "each node runs under the profile it names"),
)


def graph_flag_refusal(
    *,
    playbook: Any = None,
    file: Any = None,
    reactive: Any = None,
    resume: Any = None,
    with_synthesis: Any = False,
    bare: Any = False,
) -> str | None:
    """Why ``--graph`` cannot be combined with the other options given, or None."""
    given = {
        "playbook": playbook,
        "file": file,
        "resume": resume,
        "with_synthesis": with_synthesis,
        "bare": bare,
    }
    for key, flag, reason in _GRAPH_FLAG_REFUSALS:
        if given[key]:
            return f"--graph cannot be combined with {flag}: {reason}"
    if reactive is not None and reactive != "off":
        return (
            f"--graph cannot be combined with --reactive {reactive}: a written graph's "
            "topology is fixed at submit, so reactive growth is off"
        )
    return None


def write_graph_copy(directory: str | os.PathLike, source: GraphSource) -> Path:
    """Write *source*'s accepted bytes to ``graph.json`` in *directory*, owner-only.

    The file is created exclusively. A copy already there holding the same bytes (the same
    run directory written twice) is kept; any other file there is refused.
    """
    target = Path(directory) / GRAPH_COPY_FILENAME
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        existing = _read_copy_bytes(target)
        if existing is not None and hashlib.sha256(existing).hexdigest() == source.sha256:
            return target
        raise FlowGraphError(
            "copy", f"{target} already exists and does not hold this graph"
        ) from None
    try:
        view = memoryview(source.data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    return target


def _read_copy_bytes(path: Path) -> bytes | None:
    """The bytes of a graph copy, read without following a link, or None when unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP, errno.EMLINK, errno.EACCES):
            return None
        raise
    try:
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_GRAPH_BYTES:
            chunk = os.read(fd, MAX_GRAPH_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def load_graph_record(record: Any, run_state_dir: str | os.PathLike) -> GraphSource:
    """Check a checkpoint's ``graph`` field against the copy in the run's directory.

    The copy is read with the same loader a submit uses; its digest must equal the
    recorded one, so an edit after submit reaches no resume. The returned source names the
    path and resolved path the record names.
    """
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise FlowGraphError("record", "the checkpoint's graph record is malformed")
    digest, given, resolved = record["sha256"], record["path"], record["resolved"]
    if not (isinstance(digest, str) and len(digest) == 64 and set(digest) <= _HEX):
        raise FlowGraphError("record", "the checkpoint's graph record has no valid sha256")
    for value in (given, resolved):
        if not (isinstance(value, str) and value and Path(value).is_absolute()):
            raise FlowGraphError("record", "the checkpoint's graph record has no absolute path")
    copy = Path(run_state_dir) / GRAPH_COPY_FILENAME
    try:
        source = load_graph(str(copy), given_path=given, resolved=resolved)
    except FlowGraphError as exc:
        raise FlowGraphError(
            "copy", f"the run's graph copy {str(copy)!r} cannot be read: {exc}"
        ) from None
    if source.sha256 != digest:
        raise FlowGraphError(
            "digest",
            f"the run's graph copy {str(copy)!r} does not match the digest its checkpoint "
            "recorded; it changed after the run was submitted",
        )
    return source
