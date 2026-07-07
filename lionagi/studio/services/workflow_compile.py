# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Compile a Studio WorkflowDef spec into an executable lionagi OperationGraph.

The only new security surface here is `StudioExprCondition` — a restricted-grammar
expression evaluator for edge conditions authored in the Studio designer. It never
calls eval/exec/compile/__import__; the AST is walked against a closed node-type
allowlist before it is ever evaluated.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field, PrivateAttr

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.graph.edge import Edge, EdgeCondition
from lionagi.protocols.graph.graph import Graph

__all__ = (
    "StudioExprCondition",
    "UnsafeExpressionError",
    "WorkflowCompileError",
    "EXECUTABLE_NODE_KINDS",
    "DROPPED_NODE_KINDS",
    "compile_workflow_def",
    "build_early_graph",
    "make_engine_operation",
)


EXECUTABLE_NODE_KINDS: frozenset[str] = frozenset({"input", "chat", "engine"})
DROPPED_NODE_KINDS: frozenset[str] = frozenset({"parse", "fanout", "gate"})

_MAX_EXPR_LEN = 1000
_MAX_AST_DEPTH = 20

_ALLOWED_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
_ALLOWED_BOOL_OPS = (ast.And, ast.Or)


class UnsafeExpressionError(ValueError):
    """A condition expression failed the restricted-grammar safety check."""


class WorkflowCompileError(Exception):
    """A WorkflowDef spec could not compile to an executable graph.

    Carries the offending node/edge id so callers (the run route, the designer
    UI) can annotate the error in place instead of surfacing a bare 500.
    """

    def __init__(
        self, message: str, *, node_id: str | None = None, edge_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.node_id = node_id
        self.edge_id = edge_id

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "node_id": self.node_id, "edge_id": self.edge_id}


# ─── Safe expression grammar ───────────────────────────────────────────────
#
# Allowed: comparisons (== != < <= > >=), boolean and/or/not, literals
# (str/int/float/bool/None), list/tuple literals of the above, names,
# attribute access, subscript/key access, in/not in. Everything else
# (calls, lambdas, comprehensions, f-strings, walrus, imports, dunder
# names/attributes) is rejected before the tree is ever evaluated.


def _check_depth(node: ast.AST, depth: int = 0) -> None:
    if depth > _MAX_AST_DEPTH:
        raise UnsafeExpressionError(f"expression exceeds max nesting depth ({_MAX_AST_DEPTH})")
    for child in ast.iter_child_nodes(node):
        _check_depth(child, depth + 1)


def _validate_node(node: ast.AST) -> None:
    """Raise UnsafeExpressionError for any construct outside the allowed grammar."""
    if isinstance(node, ast.Expression):
        _validate_node(node.body)
        return
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, _ALLOWED_BOOL_OPS):
            raise UnsafeExpressionError(f"boolean operator {type(node.op).__name__} not allowed")
        for value in node.values:
            _validate_node(value)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            raise UnsafeExpressionError(f"unary operator {type(node.op).__name__} not allowed")
        _validate_node(node.operand)
        return
    if isinstance(node, ast.Compare):
        _validate_node(node.left)
        for op in node.ops:
            if not isinstance(op, _ALLOWED_COMPARE_OPS):
                raise UnsafeExpressionError(f"comparison operator {type(op).__name__} not allowed")
        for comparator in node.comparators:
            _validate_node(comparator)
        return
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise UnsafeExpressionError(f"attribute access to {node.attr!r} is not allowed")
        _validate_node(node.value)
        return
    if isinstance(node, ast.Subscript):
        _validate_node(node.value)
        _validate_node(node.slice)
        return
    if isinstance(node, ast.Name):
        if node.id.startswith("_"):
            raise UnsafeExpressionError(f"name {node.id!r} is not allowed")
        return
    if isinstance(node, ast.Constant):
        if node.value is not None and not isinstance(node.value, str | int | float | bool):
            raise UnsafeExpressionError(f"literal of type {type(node.value).__name__} not allowed")
        return
    if isinstance(node, ast.List | ast.Tuple):
        for element in node.elts:
            _validate_node(element)
        return
    raise UnsafeExpressionError(f"expression construct {type(node).__name__} is not allowed")


def _parse_expr(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise UnsafeExpressionError("condition expression must be a non-empty string")
    if len(expr) > _MAX_EXPR_LEN:
        raise UnsafeExpressionError(
            f"condition expression exceeds max length ({_MAX_EXPR_LEN} chars)"
        )
    try:
        tree = ast.parse(expr, mode="eval")
        _check_depth(tree)
        _validate_node(tree)
    except SyntaxError as exc:
        raise UnsafeExpressionError(f"condition expression is not valid syntax: {exc}") from exc
    except RecursionError as exc:
        raise UnsafeExpressionError("condition expression is too deeply nested") from exc
    return tree


def _apply_compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    raise UnsafeExpressionError(f"comparison operator {type(op).__name__} not allowed")


def _eval_node(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ctx)
    if isinstance(node, ast.BoolOp):
        result: Any = isinstance(node.op, ast.And)
        for value in node.values:
            result = _eval_node(value, ctx)
            if isinstance(node.op, ast.And) and not result:
                return result
            if isinstance(node.op, ast.Or) and result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):  # only ast.Not is allowed past _validate_node
        return not _eval_node(node.operand, ctx)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, ctx)
            if not _apply_compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Attribute):
        base = _eval_node(node.value, ctx)
        if isinstance(base, dict):
            return base.get(node.attr)
        return getattr(base, node.attr, None)
    if isinstance(node, ast.Subscript):
        base = _eval_node(node.value, ctx)
        key = _eval_node(node.slice, ctx)
        try:
            return base[key]
        except (KeyError, IndexError, TypeError):
            return None
    if isinstance(node, ast.Name):
        if node.id not in ctx:
            raise UnsafeExpressionError(f"name {node.id!r} is not defined in the condition context")
        return ctx[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_eval_node(element, ctx) for element in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(element, ctx) for element in node.elts)
    raise UnsafeExpressionError(f"expression construct {type(node).__name__} is not allowed")


class StudioExprCondition(EdgeCondition):
    """Restricted-grammar edge condition compiled from a Studio WorkflowEdge.condition string.

    Evaluated strictly against ``{"result": <upstream operation result>,
    "context": <flow context>}`` — no builtins, no globals, no calls.
    """

    expr: str = Field(...)
    _tree: Any = PrivateAttr(default=None)

    def __init__(self, **data: Any) -> None:
        # Parse BEFORE pydantic's own validation machinery runs: a plain
        # model_validator would still raise UnsafeExpressionError, but pydantic
        # wraps every validator exception into pydantic_core.ValidationError,
        # which callers matching `except UnsafeExpressionError` (this class's
        # documented contract, and compile_workflow_def's edge-id mapping)
        # would silently fail to catch.
        tree = _parse_expr(data.get("expr", ""))
        super().__init__(**data)
        self._tree = tree

    async def apply(self, context: Any = None, *args: Any, **kwargs: Any) -> bool:
        tree = self._tree if self._tree is not None else _parse_expr(self.expr)
        ctx = context if isinstance(context, dict) else {}
        return bool(_eval_node(tree, ctx))


# ─── Compile: WorkflowDef spec → OperationGraph ────────────────────────────


async def compile_workflow_def(
    spec: dict[str, Any],
    *,
    resolve_engine_def: Callable[[str], Awaitable[dict[str, Any] | None]],
) -> tuple[Graph, dict[str, str]]:
    """Compile a validated WorkflowDef spec into an executable Graph.

    Returns ``(graph, id_map)`` where ``id_map`` maps authored node ids to the
    internal Operation ids lionagi assigned them. Raises WorkflowCompileError
    (node_id/edge_id set) on any problem — never lets a bad expr, unknown
    engine_def_id, or a parse/fanout/gate node reach the executor.
    """
    nodes: list[dict[str, Any]] = spec.get("nodes", [])
    edges: list[dict[str, Any]] = spec.get("edges", [])

    for n in nodes:
        kind = n.get("kind")
        if kind in DROPPED_NODE_KINDS:
            raise WorkflowCompileError(
                f"node kind {kind!r} is not executable in v1 "
                "(parse/fanout/gate were dropped — see the workflow-exec spec)",
                node_id=n.get("id"),
            )
        if kind not in EXECUTABLE_NODE_KINDS:
            raise WorkflowCompileError(f"unknown node kind {kind!r}", node_id=n.get("id"))

    builder = OperationGraphBuilder("studio-workflow")
    id_map: dict[str, str] = {}

    for n in nodes:
        kind = n["kind"]
        node_id = n["id"]
        if kind == "input":
            # Compile-time marker only — not an Operation. Its data reaches every
            # op uniformly via session.flow(context=...), so no graph edge is needed.
            continue

        config = n.get("config") or {}
        if kind == "chat":
            prompt = config.get("prompt")
            if not prompt or not isinstance(prompt, str):
                raise WorkflowCompileError("chat node requires config.prompt", node_id=node_id)
            op_id = builder.add_operation("chat", node_id=node_id, instruction=prompt)
        elif kind == "engine":
            engine_def_id = config.get("engine_def_id")
            if not engine_def_id or not isinstance(engine_def_id, str):
                raise WorkflowCompileError(
                    "engine node requires config.engine_def_id", node_id=node_id
                )
            defn = await resolve_engine_def(engine_def_id)
            if defn is None:
                raise WorkflowCompileError(
                    f"unknown engine_def_id {engine_def_id!r}", node_id=node_id
                )
            op_id = builder.add_operation(
                "engine",
                node_id=node_id,
                engine_kind=defn.get("kind"),
                engine_model=config.get("model") or defn.get("model"),
                engine_max_depth=config.get("max_depth", defn.get("max_depth")),
                engine_max_agents=config.get("max_agents", defn.get("max_agents")),
                engine_options={
                    **(defn.get("options") or {}),
                    **(config.get("options") or {}),
                },
            )
        else:  # pragma: no cover — unreachable, prefiltered above
            raise WorkflowCompileError(f"unknown node kind {kind!r}", node_id=node_id)

        # Edges are wired by hand below from WorkflowEdge, not the builder's
        # own depends_on/sequential auto-chaining — sever it after every node.
        builder._current_heads = []
        id_map[node_id] = op_id

    graph = builder.graph

    for e in edges:
        edge_id = e.get("id")
        src_wf, dst_wf = e.get("from"), e.get("to")
        dst_op = id_map.get(dst_wf)
        if dst_op is None:
            raise WorkflowCompileError(
                f"edge target {dst_wf!r} is not an executable node", edge_id=edge_id
            )
        src_op = id_map.get(src_wf)
        if src_op is None:
            # Source is an 'input' node — no Operation-level edge is created.
            # A condition on such an edge cannot gate anything (the edge is
            # dropped), so the target would run unconditionally — silently
            # ignoring the guard. Reject it rather than compile a misleading
            # unconditional run; gating on input must go through an
            # intermediate executable node that carries the condition.
            if e.get("condition"):
                raise WorkflowCompileError(
                    "a condition on an edge from an 'input' node is not "
                    "supported (the edge carries no runtime gate); gate via an "
                    "intermediate node instead",
                    edge_id=edge_id,
                )
            continue

        condition = None
        expr = e.get("condition")
        if expr:
            try:
                condition = StudioExprCondition(expr=expr)
            except UnsafeExpressionError as exc:
                raise WorkflowCompileError(str(exc), edge_id=edge_id) from exc

        label = [e["label"]] if e.get("label") else []
        graph.add_edge(Edge(head=src_op, tail=dst_op, condition=condition, label=label))

    if not graph.is_acyclic():
        raise WorkflowCompileError(
            "workflow contains a cycle; loops are not supported in v1 "
            "(iteration lives inside a node, not as a graph cycle)"
        )

    return graph, id_map


def build_early_graph(spec: dict[str, Any]) -> dict[str, Any]:
    """Build the authored-graph shape stored at session.node_metadata.early_graph.

    Matches the frontend's WorkerGraph shape (WorkerStepNode/WorkerLinkEdge) so
    the existing run-detail renderer (sessions.py:_graph_from_metadata ->
    WorkerCanvas) can draw it without any new frontend code.
    """
    nodes: list[dict[str, Any]] = []
    for n in spec.get("nodes", []):
        if n.get("kind") not in EXECUTABLE_NODE_KINDS:
            continue
        config = n.get("config") or {}
        kind = n["kind"]
        if kind == "chat":
            assignment = config.get("model") or ""
            prompt = config.get("prompt") or ""
        elif kind == "engine":
            assignment = config.get("engine_def_id") or ""
            prompt = ""
        else:
            assignment = ""
            prompt = ""
        nodes.append(
            {
                "id": n["id"],
                "label": n.get("label") or n["id"],
                "role": kind,
                "assignment": assignment,
                "prompt": prompt,
                "capacity": 1,
                "timeout": None,
                "inputs": [],
                "outputs": [],
            }
        )

    node_ids = {n["id"] for n in nodes}
    by_id = {n["id"]: n for n in nodes}
    edges: list[dict[str, Any]] = []
    for e in spec.get("edges", []):
        src, dst = e.get("from"), e.get("to")
        if src not in node_ids or dst not in node_ids:
            continue
        condition = e.get("condition")
        edges.append(
            {
                "id": e["id"],
                "source": src,
                "target": dst,
                "mode": "code" if condition else "simple",
                "condition": condition,
            }
        )
        by_id[dst]["inputs"].append(src)
        by_id[src]["outputs"].append(dst)

    return {"nodes": nodes, "edges": edges}


# ─── The 'engine' Operation kind ────────────────────────────────────────────


def _derive_engine_input(context: dict[str, Any] | None) -> str:
    """Heuristic mapping from upstream flow context to an engine's main positional input.

    Prefers an upstream predecessor's textual result (the ``{pred_id}_result``
    keys `_prepare_operation` injects); falls back to the flow-level inputs.
    Underspecified in the spec (WorkflowEngineConfig has no explicit prompt/
    input-mapping field) — this is the compile-time call made for v1.
    """
    if not context:
        return ""
    for key, value in context.items():
        if key.endswith("_result") and value:
            return value if isinstance(value, str) else json.dumps(value, default=str)
    parts = [str(v) for v in context.values() if isinstance(v, str | int | float)]
    if parts:
        return "\n".join(parts)
    return json.dumps(context, default=str)


def make_engine_operation(session: Any) -> Callable[..., Awaitable[Any]]:
    """Build the 'engine' Branch-operation closure for one workflow run.

    Resolves the engine class per kind (reusing the CLI's kind registry —
    FindExisting, not a new one) and runs it in-process against the SAME
    session, so any sub-agent branches it spawns are wired into this run.
    """

    async def _engine_op(
        context: dict[str, Any] | None = None,
        engine_kind: str = "",
        engine_model: str | None = None,
        engine_max_depth: int | None = None,
        engine_max_agents: int | None = None,
        engine_options: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> Any:
        from lionagi.cli.engine import _KIND_META, _import_engine_class

        meta = _KIND_META.get(engine_kind)
        if meta is None:
            raise RuntimeError(f"unknown engine kind {engine_kind!r}")
        module, cls_name = meta["cls_path"]
        engine_class = _import_engine_class(module, cls_name)

        engine_kwargs: dict[str, Any] = {}
        if engine_model:
            engine_kwargs["model"] = engine_model
        if engine_max_depth is not None:
            engine_kwargs["max_depth"] = engine_max_depth
        if engine_max_agents is not None:
            engine_kwargs["max_agents"] = engine_max_agents

        options = engine_options or {}
        run_kwargs: dict[str, Any] = {}
        if engine_kind == "coding":
            run_kwargs["test_cmd"] = options.get("test_cmd")
        if engine_kind in ("coding", "hypothesis") and options.get("export_dir"):
            run_kwargs["export_dir"] = options["export_dir"]

        spec_input = _derive_engine_input(context)

        engine = engine_class(**engine_kwargs)
        result = await engine.run(spec_input, session=session, **run_kwargs)

        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        if isinstance(result, str):
            return {"result": result}
        return {"result": str(result)}

    return _engine_op
