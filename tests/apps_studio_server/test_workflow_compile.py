# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for StudioExprCondition (safe expression evaluator) and compile_workflow_def."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.studio.services.workflow_compile import (
    StudioExprCondition,
    UnsafeExpressionError,
    WorkflowCompileError,
    build_early_graph,
    compile_workflow_def,
)

# ─── StudioExprCondition: allowed grammar ───────────────────────────────────


async def test_eq_comparison_true():
    cond = StudioExprCondition(expr="result == 'ok'")
    assert await cond.apply({"result": "ok", "context": {}}) is True


async def test_eq_comparison_false():
    cond = StudioExprCondition(expr="result == 'ok'")
    assert await cond.apply({"result": "no", "context": {}}) is False


async def test_and_or_not():
    expr = "not (result == 'a') and (context['level'] >= 2 or result == 'b')"
    cond = StudioExprCondition(expr=expr)
    assert await cond.apply({"result": "b", "context": {"level": 0}}) is True
    assert await cond.apply({"result": "a", "context": {"level": 5}}) is False


async def test_attribute_access_on_object():
    class R:
        status = "done"

    cond = StudioExprCondition(expr="result.status == 'done'")
    assert await cond.apply({"result": R(), "context": {}}) is True


async def test_attribute_access_on_dict_uses_get_semantics():
    cond = StudioExprCondition(expr="result.missing == None")
    assert await cond.apply({"result": {}, "context": {}}) is True


async def test_subscript_access():
    cond = StudioExprCondition(expr="result['status'] == 'done'")
    assert await cond.apply({"result": {"status": "done"}, "context": {}}) is True


async def test_in_not_in():
    cond = StudioExprCondition(expr="result in ['a', 'b', 'c']")
    assert await cond.apply({"result": "b", "context": {}}) is True
    assert await cond.apply({"result": "z", "context": {}}) is False

    cond2 = StudioExprCondition(expr="result not in ['a']")
    assert await cond2.apply({"result": "z", "context": {}}) is True


async def test_non_dict_context_normalized_to_empty_dict():
    # apply() never crashes on an odd `context` shape from the executor — it
    # normalizes to {}, so a name lookup then fails the same way an undefined
    # name would (never silently falls back to builtins/globals).
    cond = StudioExprCondition(expr="result == 'ok'")
    with pytest.raises(UnsafeExpressionError):
        await cond.apply(None)


# ─── Hostile inputs — MUST reject at construction, never crash ─────────────

HOSTILE_EXPRS = [
    "__class__",
    "result.__class__",
    "().__class__.__bases__",
    "result.__class__.__bases__[0]",
    "__import__('os').system('echo hi')",
    "__builtins__",
    "result.__globals__",
    "(lambda: 1)()",
    "[x for x in range(10)]",
    "eval('1')",
    "exec('1')",
    "compile('1', '<s>', 'eval')",
    "result.__init__.__globals__['__builtins__']",
]


@pytest.mark.parametrize("expr", HOSTILE_EXPRS)
def test_hostile_expressions_rejected_at_construction(expr):
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr=expr)


def test_deeply_nested_expression_rejected():
    expr = "not " * 40 + "True"
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr=expr)


def test_huge_expression_rejected():
    expr = "'" + ("a" * 5_000_000) + "' == 'x'"
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr=expr)


def test_empty_expression_rejected():
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr="")


def test_whitespace_only_expression_rejected():
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr="   ")


def test_syntax_error_rejected_not_crashed():
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr="result ==")


def test_walrus_rejected():
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr="(x := 1) == 1")


def test_fstring_rejected():
    with pytest.raises(UnsafeExpressionError):
        StudioExprCondition(expr="f'{result}' == 'ok'")


def test_name_not_in_context_rejected_at_eval_not_construction():
    # 'foo' is a syntactically fine Name — only rejected when evaluated
    # against a context that doesn't define it (never falls back to globals).
    cond = StudioExprCondition(expr="foo == 'bar'")
    import asyncio

    with pytest.raises(UnsafeExpressionError):
        asyncio.run(cond.apply({"result": "x", "context": {}}))


# ─── compile_workflow_def ────────────────────────────────────────────────────


def _make_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "version": 1,
        "nodes": [
            {"id": "n1", "kind": "input", "label": "Input", "pos": {"x": 0, "y": 0}},
            {
                "id": "n2",
                "kind": "chat",
                "label": "Chat",
                "pos": {"x": 100, "y": 0},
                "config": {"prompt": "Summarize the input."},
            },
            {
                "id": "n3",
                "kind": "engine",
                "label": "Research",
                "pos": {"x": 200, "y": 0},
                "config": {"engine_def_id": "def-1"},
            },
        ],
        "edges": [
            {"id": "e1", "from": "n1", "to": "n2"},
            {"id": "e2", "from": "n2", "to": "n3", "condition": "result == 'go'"},
        ],
        "inputs": ["query"],
        "outputs": ["report"],
    }
    spec.update(overrides)
    return spec


async def _resolve_ok(ref: str) -> dict[str, Any]:
    return {"kind": "research", "model": None, "options": {}}


async def test_compile_basic_graph():
    graph, id_map = await compile_workflow_def(_make_spec(), resolve_engine_def=_resolve_ok)
    assert set(id_map) == {"n2", "n3"}  # 'input' node is not an Operation
    assert len(graph.internal_nodes) == 2
    assert len(graph.internal_edges) == 1  # only n2->n3; n1 is 'input', dropped


async def test_compile_merges_node_engine_options_over_def():
    """A node's config.options override the referenced EngineDef's options
    (node wins) while def-only keys are preserved — otherwise a per-node
    test_cmd/export_dir override is silently discarded.
    """

    async def _resolve_with_opts(ref: str) -> dict[str, Any]:
        return {
            "kind": "research",
            "model": None,
            "options": {"test_cmd": "def_cmd", "export_dir": "def_dir"},
        }

    spec = _make_spec()
    spec["nodes"][2]["config"]["options"] = {"test_cmd": "node_cmd"}

    graph, _id_map = await compile_workflow_def(spec, resolve_engine_def=_resolve_with_opts)

    from lionagi.operations.node import Operation

    engine_ops = [
        n
        for n in graph.internal_nodes.values()
        if isinstance(n, Operation) and n.operation == "engine"
    ]
    assert len(engine_ops) == 1
    assert engine_ops[0].parameters["engine_options"] == {
        "test_cmd": "node_cmd",  # node override wins
        "export_dir": "def_dir",  # def-only key preserved
    }


@pytest.mark.parametrize(
    "unsafe_cmd",
    [
        "pytest; rm -rf /",  # shell metacharacter injection
        "--config=/etc/passwd",  # leading-dash CLI-flag injection
    ],
)
async def test_compile_rejects_unsafe_node_engine_options(unsafe_cmd):
    """A node's config.options override the def's stored options, but those
    author-supplied values never passed engine_defs' validation. An unsafe
    test_cmd (shell metacharacters, leading-dash flag) reaches a coding
    engine's command verbatim, so the compiler must re-validate the merged
    options and reject it — otherwise a saved workflow smuggles shell control
    past the engine-def safeguards.
    """

    async def _resolve_coding(ref: str) -> dict[str, Any]:
        return {"kind": "coding", "model": None, "options": {"test_cmd": "pytest"}}

    spec = _make_spec()
    spec["nodes"][2]["config"]["options"] = {"test_cmd": unsafe_cmd}
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_coding)
    assert exc_info.value.node_id == "n3"


@pytest.mark.parametrize(
    "bad_budget", [{"max_agents": 9999}, {"max_depth": 0}, {"max_agents": "5"}]
)
async def test_compile_rejects_out_of_range_engine_budget(bad_budget):
    """A node-level max_depth/max_agents override bypasses the EngineDef's
    [1, 100]/int checks and reaches Engine(...) directly — a saved workflow
    could spawn far more agents or recurse deeper than the def permits. The
    compiler must re-validate the effective budget and reject it.
    """
    spec = _make_spec()
    spec["nodes"][2]["config"].update(bad_budget)
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.node_id == "n3"


async def test_compile_null_budget_override_falls_back_to_def():
    """An explicit null override must not discard the def's stricter budget."""

    async def _resolve_with_budget(ref: str) -> dict[str, Any]:
        return {"kind": "research", "model": None, "options": {}, "max_agents": 5}

    spec = _make_spec()
    spec["nodes"][2]["config"]["max_agents"] = None

    graph, _id_map = await compile_workflow_def(spec, resolve_engine_def=_resolve_with_budget)

    from lionagi.operations.node import Operation

    engine_op = next(
        n
        for n in graph.internal_nodes.values()
        if isinstance(n, Operation) and n.operation == "engine"
    )
    assert engine_op.parameters["engine_max_agents"] == 5  # def value, not None


async def test_compile_non_mapping_engine_options_raises_with_node_id():
    """A non-mapping config.options would raise TypeError on the ** merge,
    escaping the ValueError wrapper as a 500. Reject it as a compile error.
    """
    spec = _make_spec()
    spec["nodes"][2]["config"]["options"] = "bad"
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.node_id == "n3"


async def test_compile_non_mapping_config_raises_with_node_id():
    """A node whose config is not a mapping (e.g. a bare string) must surface a
    WorkflowCompileError with the node id, not an unstructured AttributeError —
    otherwise the run route returns 500 instead of the structured 422.
    """
    spec = _make_spec()
    spec["nodes"][2]["config"] = "bad"
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.node_id == "n3"


async def test_compile_edge_condition_is_studio_expr_condition():
    graph, id_map = await compile_workflow_def(_make_spec(), resolve_engine_def=_resolve_ok)
    edge = next(iter(graph.internal_edges))
    assert isinstance(edge.condition, StudioExprCondition)
    assert edge.condition.expr == "result == 'go'"


async def test_compile_no_spurious_auto_chain_edge():
    # Regression guard: OperationGraphBuilder.add_operation auto-chains a
    # "sequential" edge from _current_heads when depends_on is falsy. Two
    # nodes with NO edge between them in the spec must compile to zero edges.
    spec = _make_spec(edges=[])
    graph, _id_map = await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert len(graph.internal_edges) == 0


@pytest.mark.parametrize("kind", ["parse", "fanout", "gate"])
async def test_compile_dropped_kind_raises(kind):
    spec = _make_spec()
    spec["nodes"].append({"id": "n4", "kind": kind, "label": "D", "pos": {"x": 0, "y": 0}})
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.node_id == "n4"


async def test_compile_unknown_kind_raises():
    spec = _make_spec()
    spec["nodes"].append({"id": "n4", "kind": "teleport", "label": "?", "pos": {"x": 0, "y": 0}})
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.node_id == "n4"


async def test_compile_unknown_engine_def_raises_with_node_id():
    async def _resolve_none(ref: str) -> None:
        return None

    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(_make_spec(), resolve_engine_def=_resolve_none)
    assert exc_info.value.node_id == "n3"


async def test_compile_missing_engine_def_id_raises():
    spec = _make_spec()
    spec["nodes"][2]["config"] = {}
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.node_id == "n3"


async def test_compile_missing_chat_prompt_raises():
    spec = _make_spec()
    spec["nodes"][1]["config"] = {}
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.node_id == "n2"


async def test_compile_bad_condition_raises_with_edge_id():
    spec = _make_spec()
    spec["edges"][1]["condition"] = "__import__('os')"
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.edge_id == "e2"


async def test_compile_edge_to_unknown_target_raises_with_edge_id():
    spec = _make_spec()
    spec["edges"].append({"id": "e3", "from": "n3", "to": "ghost"})
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.edge_id == "e3"


async def test_compile_condition_on_input_edge_is_rejected():
    """A condition on an edge from an 'input' node is dropped with the edge, so
    it cannot gate the target — the target would run unconditionally. The
    compiler must reject it (with the edge id) rather than silently ignore it.
    """
    spec = _make_spec()
    spec["edges"][0]["condition"] = "context['enabled'] == True"  # e1: n1(input) -> n2
    with pytest.raises(WorkflowCompileError) as exc_info:
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)
    assert exc_info.value.edge_id == "e1"


async def test_compile_cycle_raises():
    spec = _make_spec()
    spec["edges"].append({"id": "e3", "from": "n3", "to": "n2"})  # n2->n3->n2
    with pytest.raises(WorkflowCompileError, match="cycle"):
        await compile_workflow_def(spec, resolve_engine_def=_resolve_ok)


# ─── build_early_graph ───────────────────────────────────────────────────────


def test_build_early_graph_shape():
    early = build_early_graph(_make_spec())
    ids = {n["id"] for n in early["nodes"]}
    assert ids == {"n1", "n2", "n3"}  # 'input' IS included for display
    edge_ids = {e["id"] for e in early["edges"]}
    assert edge_ids == {"e1", "e2"}
    e2 = next(e for e in early["edges"] if e["id"] == "e2")
    assert e2["condition"] == "result == 'go'"
    assert e2["mode"] == "code"
    e1 = next(e for e in early["edges"] if e["id"] == "e1")
    assert e1["mode"] == "simple"


def test_build_early_graph_drops_non_executable_nodes():
    spec = _make_spec()
    spec["nodes"].append({"id": "n4", "kind": "gate", "label": "G", "pos": {"x": 0, "y": 0}})
    spec["edges"].append({"id": "e3", "from": "n3", "to": "n4"})
    early = build_early_graph(spec)
    ids = {n["id"] for n in early["nodes"]}
    assert "n4" not in ids
    edge_ids = {e["id"] for e in early["edges"]}
    assert "e3" not in edge_ids  # dangles to a dropped node
