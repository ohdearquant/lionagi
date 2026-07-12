# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Conformance suite pinning every graph-shaped production surface to the
Session.flow / streaming-kernel execution authority.

A ``GraphSurface`` manifest below classifies every graph-shaped function this
suite can find in the shipped ``lionagi`` package: whether it delegates to
``Session.flow`` (directly or through an adapter), reaches the sanctioned
streaming kernel, is itself the kernel, or is a pure builder/alias that never
executes anything. ``test_every_ast_discovered_graph_candidate_is_registered``
scans the real source tree for qualified ``.flow``/``.flow_stream``/
``.run_dag`` calls, bare calls to a locally-imported kernel function (the
shape ``Session.flow``/``Session.flow_stream`` themselves use), and
executor/graph-builder construction sites, and fails loudly (printing
file:line and why it was flagged) if it finds one that is not in the
manifest — so adding a new graph entrypoint without registering it here
breaks the build instead of silently growing a second executor.

The executor-construction scan statically recognizes: a direct
``DependencyAwareExecutor(...)``/``ReactiveExecutor(...)`` call; a
``from ... import DependencyAwareExecutor as X`` import alias; a bare
assignment alias one level deep (``X = DependencyAwareExecutor`` or
``X = <existing alias>``), with imports and assignments replayed in one
combined source-ordered pass so a later genuine import restores provenance
an earlier unrelated rebinding discarded (and a later rebinding still drops
provenance a prior import established, whichever comes last wins); and a
literal ``getattr(<flow module>, "DependencyAwareExecutor")`` dynamic-lookup
call — recognized only when the receiver statically denotes
``lionagi.operations.flow`` — whether assigned to a name first or called
inline. Import provenance is tracked per lexical scope, not as one flat
whole-module timeline: each function/lambda scope starts from a copy of its
enclosing scope's provenance with parameter-shadowed names discarded, then
replays its OWN body's binding events (in source order) on top of that copy —
so a function-local reimport genuinely restores what a same-named parameter
shadowed. Within a scope, a binding event nested inside an
``if``/``try``/``except``/``for``/``while``/``with`` block is CONDITIONAL:
it may only DISCARD provenance (a conditional rebinding to something
unrecognized is treated as if it might have executed, since trusting the name
afterward risks a false-positive executor site) and may never ESTABLISH or
RESTORE it (a conditional import might never execute, so applying it anyway
risks the same false positive from the other direction) — only an
unconditional binding, a direct statement of the scope's own body, can
establish or restore provenance. It does not perform general data-flow
analysis: resolution through a factory function's return value, a
non-literal string argument, an alias threaded through more than one
intermediate assignment, or any binding inside a class body, comprehension,
or walrus assignment is not tracked and remains a residual imprecision.

Registering a location in the manifest is necessary but not sufficient: any
row that names an ``expected_target`` must also name a ``delegation_test`` —
the exact pytest node id of the test that actually asserts the delegation
(call count, argument identity, or a mocked target being reached) — and any
row with ``persistence="required"`` must name a ``persistence_evidence`` node
id backed by a real StateDB write. Both are validated against real source
(not just checked for non-emptiness), so a stale or nonexistent reference
fails the suite instead of reading as coverage.

Known limitation of the delegation-test-id mechanism:
``test_every_delegation_test_id_resolves_to_a_real_test_function`` only
proves the named node id resolves to a real top-level test function in the
right module — it does not read what that test's body actually asserts, so
a row can cite a real, passing test that asserts an entirely different
relationship (this is exactly how the studio-engine-node row's prior
``EngineRun.run_dag`` claim went unnoticed: the cited coding-engine test is
real and passing, but only ever exercises ``engine.run``, never
``run_dag``). ``test_every_delegation_test_source_mentions_its_expected_target``
adds a loud but weak structural companion — a substring check that the
cited test's own source at least mentions a token drawn from
``expected_target`` — which would have caught that exact drift. It is a
necessary-not-sufficient tripwire, not a substitute for reading the cited
test yourself before trusting a manifest row.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from unittest import mock
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIONAGI_ROOT = REPO_ROOT / "lionagi"


# ---------------------------------------------------------------------------
# 1. Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphSurface:
    key: str
    symbol: str
    role: Literal[
        "facade",
        "streaming_facade",
        "kernel",
        "streaming_kernel",
        "adapter",
        "builder",
        "alias",
        "n/a",
        "known_violation",
    ]
    # (module_path relative to the repo root, qualname) for every surface an
    # AST/registry/export scan can independently discover. None for aliases
    # and registry entries that only exist as a mapping, not a call site.
    location: tuple[str, str] | None
    expected_target: str | None
    persistence: Literal["required", "inherited", "not_applicable", "known_violation"]
    reason: str
    persistence_evidence: str | None = None
    # "path/to/test_module.py::test_name" pytest node id of the test that
    # actually asserts the expected_target relationship (call count, argument
    # identity, or delegated-return-value). Required whenever expected_target
    # is not None -- see test_every_delegation_target_has_a_named_test below,
    # which also resolves the id against real source so a typo'd or deleted
    # reference fails loudly instead of reading as coverage.
    delegation_test: str | None = None


GRAPH_SURFACES: tuple[GraphSurface, ...] = (
    GraphSurface(
        key="session-flow",
        symbol="lionagi.session.session.Session.flow",
        location=("lionagi/session/session.py", "Session.flow"),
        role="facade",
        expected_target="operations.flow.flow",
        persistence="not_applicable",
        reason="public facade; caller owns persistence via on_branch_created/on_progress",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_session_flow_delegates_to_operations_flow_kernel_with_same_identity"
        ),
    ),
    GraphSurface(
        key="session-flow-stream",
        symbol="lionagi.session.session.Session.flow_stream",
        location=("lionagi/session/session.py", "Session.flow_stream"),
        role="streaming_facade",
        expected_target="operations.flow.flow_stream",
        persistence="not_applicable",
        reason="streaming facade; lifecycle signal translation is opt-in, not owned here",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_session_flow_stream_delegates_to_streaming_kernel_without_calling_ordinary_flow"
        ),
    ),
    GraphSurface(
        key="flow-kernel",
        symbol="lionagi.operations.flow.flow",
        location=("lionagi/operations/flow.py", "flow"),
        role="kernel",
        expected_target=None,
        persistence="not_applicable",
        reason="terminal kernel; selects and awaits exactly one executor",
    ),
    GraphSurface(
        key="flow-stream-kernel",
        symbol="lionagi.operations.flow.flow_stream",
        location=("lionagi/operations/flow.py", "flow_stream"),
        role="streaming_kernel",
        expected_target=None,
        persistence="not_applicable",
        reason="terminal streaming kernel; constructs the reactive executor and yields events",
    ),
    GraphSurface(
        key="engine-run-dag",
        symbol="lionagi.engines.engine.EngineRun.run_dag",
        location=("lionagi/engines/engine.py", "EngineRun.run_dag"),
        role="adapter",
        expected_target="Session.flow",
        persistence="inherited",
        reason="wraps the call in flow_progress_signals; persistence is caller-owned",
        persistence_evidence="tests/session/test_lifecycle_signals.py::test_run_dag_calls_session_flow_once_with_same_graph",
        delegation_test="tests/session/test_lifecycle_signals.py::test_run_dag_calls_session_flow_once_with_same_graph",
    ),
    GraphSurface(
        key="planning-engine-run",
        symbol="lionagi.engines.planning.PlanningEngine._run",
        location=("lionagi/engines/planning.py", "PlanningEngine._run"),
        role="adapter",
        expected_target="EngineRun.run_dag",
        persistence="inherited",
        reason="plans, builds the DAG, and submits through the run's run_dag; the next hop is engine-run-dag",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_planning_engine_run_delegates_to_engine_run_run_dag"
        ),
    ),
    GraphSurface(
        key="orchestration-fanout",
        symbol="lionagi.orchestration.patterns.fanout",
        location=("lionagi/orchestration/patterns.py", "fanout"),
        role="adapter",
        expected_target="session.flow",
        persistence="inherited",
        reason="library fan-out helper; builds a parallel graph and submits it via the injected session",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_orchestration_fanout_submits_built_graph_through_injected_session_flow"
        ),
    ),
    GraphSurface(
        key="cli-o-fanout",
        symbol="lionagi.cli.orchestrate.fanout._run_fanout_inner",
        location=("lionagi/cli/orchestrate/fanout.py", "_run_fanout_inner"),
        role="adapter",
        expected_target="env.session.flow",
        persistence="required",
        reason="CLI fan-out; the owning wrapper _run_fanout opens/binds live-persist StateDB state "
        "via its own start_live_persist call before _run_fanout_inner submits the graph",
        persistence_evidence=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_run_fanout_persists_session_via_start_live_persist"
        ),
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_run_fanout_inner_calls_env_session_flow_with_builder_graph"
        ),
    ),
    GraphSurface(
        key="cli-o-flow-exec",
        symbol="lionagi.cli.orchestrate.flow._execute_dag",
        location=("lionagi/cli/orchestrate/flow.py", "_execute_dag"),
        role="adapter",
        expected_target="EngineRun.run_dag",
        persistence="required",
        reason="CLI flow execution phase; the owning wrapper _run_flow opens/binds live-persist "
        "StateDB state via its own start_live_persist call before _execute_dag drives the "
        "planning engine's run_dag over the built DAG",
        persistence_evidence=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_run_flow_persists_session_via_start_live_persist"
        ),
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_execute_dag_delegates_to_planning_engine_run_dag"
        ),
    ),
    GraphSurface(
        key="cli-o-flow-synth",
        symbol="lionagi.cli.orchestrate.flow._synthesize",
        location=("lionagi/cli/orchestrate/flow.py", "_synthesize"),
        role="adapter",
        expected_target="Session.flow",
        persistence="inherited",
        reason="CLI flow synthesis phase submits the final synthesis op directly through Session.flow "
        "rather than the engine bridge; persistence setup is shared with cli-o-flow-exec",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_synthesize_calls_env_session_flow_with_builder_graph"
        ),
    ),
    GraphSurface(
        key="cli-o-flow-resume",
        symbol="lionagi.cli.orchestrate.flow._resume_flow",
        location=None,
        role="alias",
        expected_target="_run_flow",
        persistence="inherited",
        reason="resolves a checkpoint and replays it through _run_flow; not a second execution lane",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_resume_flow_calls_run_flow_with_resolved_checkpoint"
        ),
    ),
    GraphSurface(
        key="cli-play",
        symbol="lionagi.cli.main._handle_play_shortcut",
        location=None,
        role="alias",
        expected_target='["o", "flow", "-p", NAME, ...]',
        persistence="inherited",
        reason="rewrites argv to the registered `o flow` subcommand; never executes anything itself",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_play_shortcut_rewrites_to_registered_o_flow_subcommand"
        ),
    ),
    GraphSurface(
        key="li-engine-run-planning",
        symbol="lionagi.cli.engine._KIND_META['planning']",
        location=None,
        role="alias",
        expected_target="PlanningEngine.run",
        persistence="required",
        reason="CLI registry entry for the one DAG-shaped engine kind; other kinds are non-graph",
        persistence_evidence=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_engine_run_planning_kind_uses_real_planning_engine_and_persists_via_statedb"
        ),
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_engine_run_planning_kind_uses_real_planning_engine_and_persists_via_statedb"
        ),
    ),
    GraphSurface(
        key="studio-workflow-route",
        symbol="lionagi.studio.services.workflow_defs.run_workflow_def_route",
        location=None,
        role="alias",
        expected_target="run_workflow_def",
        persistence="required",
        reason="HTTP route; delegates to run_workflow_def and translates its errors to HTTP status codes",
        persistence_evidence="tests/apps_studio_server/test_workflow_run.py::test_workflow_run_persists_node_lifecycle_signals",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_studio_workflow_route_delegates_to_run_workflow_def"
        ),
    ),
    GraphSurface(
        key="run-workflow-def",
        symbol="lionagi.studio.services.workflow_run.run_workflow_def",
        location=("lionagi/studio/services/workflow_run.py", "run_workflow_def"),
        role="adapter",
        expected_target="Session.flow",
        persistence="required",
        reason="compiles the authored workflow graph and submits it through Session.flow with a "
        "request-scoped StateDB lifecycle",
        persistence_evidence="tests/apps_studio_server/test_workflow_run.py::test_workflow_run_persists_node_lifecycle_signals",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_run_workflow_def_delegates_to_session_flow_with_progress_wrapper"
        ),
    ),
    GraphSurface(
        key="studio-engine-node",
        symbol="lionagi.studio.services.workflow_compile.make_engine_operation",
        location=None,
        role="alias",
        expected_target="engine.run",
        persistence="inherited",
        reason="registers the 'engine' operation consumed by run-workflow-def; production "
        "make_engine_operation resolves whichever engine kind the node names and calls "
        "engine.run(...) generically (lionagi/studio/services/workflow_compile.py:613-643) — "
        "the cited test drives a coding-kind engine through exactly that dispatch. Only the "
        "planning kind's engine.run subsequently reaches EngineRun.run_dag, and that further hop "
        "is covered separately by planning-engine-run and engine-run-dag, not by this row",
        delegation_test=(
            "tests/apps_studio_server/test_workflow_run.py::"
            "test_workflow_run_node_cwd_reaches_engine_invocation"
        ),
    ),
    GraphSurface(
        key="operation-graph-builder",
        symbol="lionagi.operations.builder.OperationGraphBuilder",
        location=("lionagi/operations/builder.py", "OperationGraphBuilder.__init__"),
        role="builder",
        expected_target=None,
        persistence="not_applicable",
        reason="pure incremental graph builder; has no execute/flow method",
    ),
    GraphSurface(
        key="build-fanout-graph",
        symbol="lionagi.orchestration.patterns.build_fanout_graph",
        location=("lionagi/orchestration/patterns.py", "build_fanout_graph"),
        role="builder",
        expected_target=None,
        persistence="not_applicable",
        reason="pure synchronous graph compiler; callers submit the result",
    ),
    GraphSurface(
        key="build-dag-graph",
        symbol="lionagi.orchestration.patterns.build_dag_graph",
        location=("lionagi/orchestration/patterns.py", "build_dag_graph"),
        role="builder",
        expected_target=None,
        persistence="not_applicable",
        reason="pure synchronous graph compiler; callers submit the result",
    ),
    GraphSurface(
        key="cli-builder-owner",
        symbol="lionagi.cli.orchestrate._orchestration.setup_orchestration",
        location=("lionagi/cli/orchestrate/_orchestration.py", "setup_orchestration"),
        role="builder",
        expected_target=None,
        persistence="not_applicable",
        reason="allocates the shared OrchestrationEnv.builder consumed by the registered "
        "CLI fanout/flow adapters; never calls flow itself",
    ),
    GraphSurface(
        key="compile-workflow-def",
        symbol="lionagi.studio.services.workflow_compile.compile_workflow_def",
        location=("lionagi/studio/services/workflow_compile.py", "compile_workflow_def"),
        role="builder",
        expected_target=None,
        persistence="not_applicable",
        reason="compiles a WorkflowDef spec into a Graph; run_workflow_def executes the result later",
    ),
    GraphSurface(
        key="visualize-graph",
        symbol="lionagi.operations._visualize_graph.visualize_graph",
        location=None,
        role="n/a",
        expected_target=None,
        persistence="not_applicable",
        reason="reads node status off an already-executed builder for rendering; never executes",
    ),
    GraphSurface(
        key="scheduler-launch-actions",
        symbol="lionagi.studio.scheduler.subprocess.build_argv",
        location=None,
        role="alias",
        expected_target='["o", "flow"] or ["o", "fanout"]',
        persistence="inherited",
        reason="builds subprocess argv for the registered `li o flow`/`li o fanout` subcommands; "
        "the spawned child owns its own flow persistence",
        delegation_test=(
            "tests/operations/test_graph_entrypoint_conformance.py::"
            "test_scheduler_build_argv_only_dispatches_registered_o_subcommands"
        ),
    ),
    GraphSurface(
        key="reactive-callback-gap",
        symbol="lionagi.operations.flow._assign_injected_branch / ReactiveExecutor",
        location=None,
        role="known_violation",
        expected_target=None,
        persistence="known_violation",
        reason="reactive flows do not forward on_branch_created to preallocated or injected clones; "
        "see the strict-xfail regression in tests/operations/test_reactive_flow.py",
    ),
)


# ---------------------------------------------------------------------------
# 2. Discovery feeds
# ---------------------------------------------------------------------------

_ATTR_SINK_NAMES = {"flow", "flow_stream", "run_dag"}
# Bare (unqualified) names the graph-execution kernel is publicly imported
# as. A facade can do `from lionagi.operations.flow import flow` and then
# call the bound name directly (`await flow(...)`) instead of a qualified
# `.flow(...)` attribute call — Session.flow/Session.flow_stream themselves
# do exactly this. _collect_kernel_import_bindings finds the local name each
# file binds these to (honoring `as` aliases) so visit_Call can recognize
# the bare call regardless of what a facade names its own methods.
_KERNEL_FUNCTION_NAMES = {"flow", "flow_stream"}
_CONSTRUCTOR_SINK_NAMES = {
    "OperationGraphBuilder",
    "DependencyAwareExecutor",
    "ReactiveExecutor",
    "Graph",
}
_EXECUTOR_CONSTRUCTOR_NAMES = {"DependencyAwareExecutor", "ReactiveExecutor"}

# Directories under lionagi/ that are not shipped runtime entrypoints.
_EXCLUDED_DIR_NAMES = {"__pycache__"}


def _collect_kernel_import_bindings(tree: ast.AST) -> set[str]:
    """Local names bound to `lionagi.operations.flow.flow`/`flow_stream` by
    any `from ...operations.flow import ...` in *tree*, at any nesting depth
    (module-level or inside a function body, matching how Session.flow does
    its own local import). Used to recognize a bare-name call to an imported
    kernel function that a plain qualified-attribute scan would miss."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.endswith("operations.flow")
        ):
            for alias in node.names:
                if alias.name in _KERNEL_FUNCTION_NAMES:
                    bound.add(alias.asname or alias.name)
    return bound


_FLOW_MODULE_PATH = "lionagi.operations.flow"


def _dotted_name(expr: ast.expr) -> str | None:
    """Render a Name/Attribute chain as a dotted path, or None if any link
    is not a plain attribute access."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


@dataclass
class _FlowModuleEnv:
    """Import-provenance environment for recognizing expressions that
    statically denote ``lionagi.operations.flow``. Every set holds LOCAL
    names whose binding was actually observed in the module's imports (or a
    recognized ``import_module`` assignment), so a same-shaped expression
    rooted in an arbitrary object (``lionagi = plugin`` shadowing, a
    ``plugin.import_module(...)`` method) is NOT treated as the kernel
    module."""

    names: set[str]  # names bound to the flow module itself
    lionagi_roots: set[str]  # names under which the lionagi package is import-bound
    import_module_funcs: set[str]  # names bound to importlib.import_module
    importlib_mods: set[str]  # names bound to the importlib module

    @classmethod
    def empty(cls) -> _FlowModuleEnv:
        return cls(set(), set(), set(), set())

    def discard(self, name: str) -> None:
        self.names.discard(name)
        self.lionagi_roots.discard(name)
        self.import_module_funcs.discard(name)
        self.importlib_mods.discard(name)

    def copy(self) -> _FlowModuleEnv:
        """Independent copy for pushing a per-scope view (see
        ``_SinkVisitor``'s scope stack): mutating the copy via ``discard``
        must never affect the enclosing scope's environment."""
        return _FlowModuleEnv(
            set(self.names),
            set(self.lionagi_roots),
            set(self.import_module_funcs),
            set(self.importlib_mods),
        )


def _param_names(args: ast.arguments) -> set[str]:
    """Every name a function/lambda parameter list binds: positional-only,
    ordinary, keyword-only, ``*args``, and ``**kwargs``. Used to mask
    module-level import provenance that a parameter shadows for the
    duration of that function body (see ``_SinkVisitor._push_param_scope``)."""
    names = {a.arg for a in args.posonlyargs}
    names.update(a.arg for a in args.args)
    names.update(a.arg for a in args.kwonlyargs)
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _is_flow_module_expr(expr: ast.expr, env: _FlowModuleEnv) -> bool:
    """True when *expr* statically denotes the flow kernel module WITH
    import provenance: a local name import-bound to it (or bound via a
    recognized ``importlib.import_module`` assignment), the dotted module
    path rooted in an import-bound ``lionagi`` name, or an inline
    ``import_module`` call with the exact literal path whose callee is
    itself import-bound (``importlib.import_module`` or a
    ``from importlib import import_module`` name). A textual match without
    that provenance (``lionagi = plugin`` shadowing, a same-named method on
    an arbitrary object) does not qualify."""
    if isinstance(expr, ast.Name) and expr.id in env.names:
        return True
    if isinstance(expr, ast.Attribute):
        dotted = _dotted_name(expr)
        if dotted == _FLOW_MODULE_PATH:
            root = dotted.split(".", 1)[0]
            return root in env.lionagi_roots
    if isinstance(expr, ast.Call) and len(expr.args) == 1:
        callee = expr.func
        callee_ok = (isinstance(callee, ast.Name) and callee.id in env.import_module_funcs) or (
            isinstance(callee, ast.Attribute)
            and callee.attr == "import_module"
            and isinstance(callee.value, ast.Name)
            and callee.value.id in env.importlib_mods
        )
        return (
            callee_ok
            and isinstance(expr.args[0], ast.Constant)
            and expr.args[0].value == _FLOW_MODULE_PATH
        )
    return False


def _resolve_constructor_alias_rhs(
    value: ast.expr,
    bound: dict[str, str],
    flow_env: _FlowModuleEnv | None = None,
) -> str | None:
    """If *value* is a bare tracked constructor name/alias, or a literal
    ``getattr(<flow module>, "<ConstructorName>")`` dynamic-lookup call naming
    one, return the canonical constructor name it resolves to. *bound* is the
    alias map accumulated so far, so an assignment can chain through an
    already-recognized alias one level deep (``Y = X`` where ``X`` was itself
    an import or assignment alias). The ``getattr`` shape is recognized only
    when its receiver statically denotes ``lionagi.operations.flow`` (a bound
    module name, the dotted path, or a literal ``import_module`` call) --
    ``getattr(plugin, "DependencyAwareExecutor")`` on an arbitrary object is
    an unrelated same-named API, not a kernel-executor construction. Only
    these literal shapes are recognized -- general data-flow (a name threaded
    through more than one intermediate assignment, a factory function's
    return value, or a non-literal string argument) is out of scope and
    remains a residual bypass; see the module docstring."""
    if isinstance(value, ast.Name):
        if value.id in _CONSTRUCTOR_SINK_NAMES:
            return value.id
        return bound.get(value.id)
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "getattr"
        and len(value.args) == 2
        and isinstance(value.args[1], ast.Constant)
        and isinstance(value.args[1].value, str)
        and value.args[1].value in _CONSTRUCTOR_SINK_NAMES
        and flow_env is not None
        and _is_flow_module_expr(value.args[0], flow_env)
    ):
        return value.args[1].value
    return None


_BindingEvent = tuple[ast.Import | ast.ImportFrom | ast.Assign, bool]


def _collect_scope_binding_events(
    stmts: list[ast.stmt], conditional: bool, events: list[_BindingEvent]
) -> None:
    """Collect Import/ImportFrom/simple-Name-Assign binding events out of
    *stmts* -- the statement list of ONE lexical scope (a module body or a
    function body) -- tagging each with whether it is CONDITIONAL: nested
    inside an ``if``/``try``/``except``/``for``/``while``/``with`` body
    rather than a direct statement of *stmts* itself. Descends into a nested
    class body transparently (a class body is not a distinct scope for this
    analysis and was never treated as one) but stops at a nested function,
    async function, or lambda -- those get their own independent scope when
    ``_SinkVisitor`` reaches them, each replaying only its own body's events
    on top of a copy of its enclosing scope's provenance."""
    for stmt in stmts:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            events.append((stmt, conditional))
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            events.append((stmt, conditional))

        if isinstance(stmt, ast.If):
            _collect_scope_binding_events(stmt.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
        elif isinstance(stmt, ast.Try):
            _collect_scope_binding_events(stmt.body, True, events)
            for handler in stmt.handlers:
                _collect_scope_binding_events(handler.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
            _collect_scope_binding_events(stmt.finalbody, True, events)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            _collect_scope_binding_events(stmt.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
        elif isinstance(stmt, ast.While):
            _collect_scope_binding_events(stmt.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect_scope_binding_events(stmt.body, True, events)
        elif isinstance(stmt, ast.ClassDef):
            _collect_scope_binding_events(stmt.body, conditional, events)
        # FunctionDef/AsyncFunctionDef/Lambda: a new scope: stop here.


def _replay_binding_events(
    events: list[_BindingEvent], bound: dict[str, str], env: _FlowModuleEnv
) -> None:
    """Replay *events* (as collected by ``_collect_scope_binding_events``) in
    source order (by lineno, col_offset), mutating *bound*/*env* in place.

    An event that would ESTABLISH or RESTORE provenance -- an import, or an
    assignment recognized as a flow-module expression or a constructor
    alias -- is applied only when UNCONDITIONAL: a conditional import or a
    conditional alias-assignment might never execute, and trusting it anyway
    risks exactly the false-positive executor site a conditionally-dead
    branch must not produce.

    An event that would CLEAR provenance -- an assignment to anything
    unrecognized -- is ALWAYS applied, conditional or not: a conditional
    rebinding might genuinely execute, and the conservative, false-positive-
    safe choice is to stop trusting the name rather than assume the
    rebinding didn't happen.

    Net effect: within one scope, unconditional bindings behave exactly like
    the old flat whole-module pass (source-order, last-one-wins); a
    conditional import/alias-assignment is a no-op that neither restores nor
    disturbs whatever provenance already held; a conditional rebinding to an
    unrecognized value still clears it."""
    for node, conditional in sorted(events, key=lambda pair: (pair[0].lineno, pair[0].col_offset)):
        if isinstance(node, ast.ImportFrom):
            if conditional:
                continue
            for alias in node.names:
                if alias.name in _CONSTRUCTOR_SINK_NAMES:
                    bound[alias.asname or alias.name] = alias.name
            if node.module is not None:
                for alias in node.names:
                    if f"{node.module}.{alias.name}" == _FLOW_MODULE_PATH:
                        env.names.add(alias.asname or alias.name)
                    elif node.module == "importlib" and alias.name == "import_module":
                        env.import_module_funcs.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            if conditional:
                continue
            for alias in node.names:
                if alias.name == _FLOW_MODULE_PATH and alias.asname:
                    env.names.add(alias.asname)
                elif alias.name.split(".", 1)[0] == "lionagi" and not alias.asname:
                    env.lionagi_roots.add("lionagi")
                elif alias.name == "importlib":
                    env.importlib_mods.add(alias.asname or "importlib")
        else:
            target = node.targets[0].id
            if _is_flow_module_expr(node.value, env):
                if conditional:
                    continue
                env.names.add(target)
                bound.pop(target, None)
                continue
            canonical = _resolve_constructor_alias_rhs(node.value, bound, env)
            if canonical is not None:
                if conditional:
                    continue
                bound[target] = canonical
            else:
                bound.pop(target, None)
                env.discard(target)


def _collect_constructor_import_bindings(tree: ast.Module) -> tuple[dict[str, str], _FlowModuleEnv]:
    """Local alias -> canonical name for any `from ... import
    <ConstructorSinkName> [as alias]` in *tree*'s MODULE scope, regardless of
    which module path it is re-exported from (``Graph`` in particular is
    re-exported from several ``lionagi.protocols.*`` modules, so this
    deliberately does not filter by source module the way
    ``_collect_kernel_import_bindings`` does), plus every simple one-level
    assignment alias (``GraphRunner = DependencyAwareExecutor`` or
    ``GraphRunner = <existing alias>``) and literal dynamic ``getattr``
    lookup alias (``GraphRunner = getattr(mod, "DependencyAwareExecutor")``)
    recognized by ``_resolve_constructor_alias_rhs``. Closes the
    aliased-constructor gap: `from lionagi.operations.flow import
    DependencyAwareExecutor as GraphRunner` followed by
    `GraphRunner(...).execute()`, a bare `GraphRunner = DependencyAwareExecutor`
    assignment, or a `getattr`-based dynamic lookup assigned to a name, must
    all be recognized as constructing ``DependencyAwareExecutor`` even though
    the call site only ever mentions the local alias.

    Only the module's OWN top-level statements (plus anything nested inside
    module-level control flow, which is CONDITIONAL -- see
    ``_collect_scope_binding_events``/``_replay_binding_events``) feed this
    pass; a binding inside a nested function/lambda body is that function's
    OWN scope and is resolved separately by ``_SinkVisitor`` when it enters
    that scope, starting from a copy of this module-level result.

    Returns ``(alias_map, flow_env)`` where the second element is the
    import-provenance environment (:class:`_FlowModuleEnv`) of names bound
    to ``lionagi.operations.flow``, the ``lionagi`` package root,
    ``importlib``, and ``import_module`` -- used to restrict ``getattr``
    recognition to receivers that provably denote the flow module."""
    bound: dict[str, str] = {}
    env = _FlowModuleEnv.empty()
    events: list[_BindingEvent] = []
    _collect_scope_binding_events(tree.body, False, events)
    _replay_binding_events(events, bound, env)
    return bound, env


class _SinkVisitor(ast.NodeVisitor):
    """Attributes qualified `.flow`/`.flow_stream`/`.run_dag` calls, bare
    calls to a locally-imported kernel function, and
    OperationGraphBuilder/executor/Graph construction calls (including
    through an aliased import, a one-level assignment alias, or a literal
    `getattr`-based dynamic lookup, assigned or called inline) to the
    innermost enclosing function or method (dotted "Class.method" or
    "function").

    Import provenance (:class:`_FlowModuleEnv`) and constructor aliases
    (``bound``) are tracked per lexical scope, not as one flat whole-module
    timeline: entering a function or lambda pushes a scoped copy of both,
    derived from the enclosing scope with any parameter-shadowed name
    discarded from each. For a function/async function (never a lambda,
    which can hold no statements), that copy is then advanced by replaying
    the function's OWN body's binding events on top of it -- via the same
    ``_collect_scope_binding_events``/``_replay_binding_events`` machinery
    ``_collect_constructor_import_bindings`` uses for the module scope -- so
    a genuine in-body reimport restores provenance a same-named parameter
    shadowed, exactly as a module-level reimport restores provenance an
    unrelated rebinding discarded. The scope is popped again on the way out,
    restoring the enclosing scope's view for sibling functions."""

    def __init__(
        self,
        kernel_names: set[str] = frozenset(),
        constructor_aliases: dict[str, str] = None,  # type: ignore[assignment]
        flow_env: _FlowModuleEnv | None = None,
    ) -> None:
        self._stack: list[str] = []
        self._kernel_names = kernel_names
        self._bound_stack: list[dict[str, str]] = [dict(constructor_aliases or {})]
        self._flow_env_stack: list[_FlowModuleEnv] = [flow_env or _FlowModuleEnv.empty()]
        self.hits: dict[str, set[str]] = {}
        self.linenos: dict[str, int] = {}
        self.executor_hits: set[str] = set()

    @property
    def _flow_env(self) -> _FlowModuleEnv:
        return self._flow_env_stack[-1]

    @property
    def _constructor_aliases(self) -> dict[str, str]:
        return self._bound_stack[-1]

    def _qualname(self) -> str:
        return ".".join(self._stack)

    def _record(self, reason: str, lineno: int, *, is_executor: bool = False) -> None:
        if not self._stack:
            return
        qualname = self._qualname()
        self.hits.setdefault(qualname, set()).add(reason)
        self.linenos[qualname] = min(self.linenos.get(qualname, lineno), lineno)
        if is_executor:
            self.executor_hits.add(qualname)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def _push_func_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        shadowed = _param_names(node.args)
        env = self._flow_env.copy()
        bound = dict(self._constructor_aliases)
        for name in shadowed:
            env.discard(name)
            bound.pop(name, None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            events: list[_BindingEvent] = []
            _collect_scope_binding_events(node.body, False, events)
            _replay_binding_events(events, bound, env)
        self._flow_env_stack.append(env)
        self._bound_stack.append(bound)

    def _pop_func_scope(self) -> None:
        self._flow_env_stack.pop()
        self._bound_stack.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self._push_func_scope(node)
        self.generic_visit(node)
        self._pop_func_scope()
        self._stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._push_func_scope(node)
        self.generic_visit(node)
        self._pop_func_scope()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _ATTR_SINK_NAMES:
            self._record(f"calls .{func.attr}()", node.lineno)
        elif isinstance(func, ast.Name) and func.id in self._kernel_names:
            self._record(f"calls imported {func.id}()", node.lineno)
        elif isinstance(func, ast.Name) and func.id in _CONSTRUCTOR_SINK_NAMES:
            self._record(
                f"constructs {func.id}()",
                node.lineno,
                is_executor=func.id in _EXECUTOR_CONSTRUCTOR_NAMES,
            )
        elif isinstance(func, ast.Name) and func.id in self._constructor_aliases:
            canonical = self._constructor_aliases[func.id]
            self._record(
                f"constructs {canonical}() (imported as {func.id})",
                node.lineno,
                is_executor=canonical in _EXECUTOR_CONSTRUCTOR_NAMES,
            )
        elif isinstance(func, ast.Attribute) and func.attr in _CONSTRUCTOR_SINK_NAMES:
            self._record(
                f"constructs {func.attr}()",
                node.lineno,
                is_executor=func.attr in _EXECUTOR_CONSTRUCTOR_NAMES,
            )
        elif isinstance(func, ast.Call):
            # Inline dynamic lookup called directly, no intermediate name:
            # getattr(importlib.import_module(...), "DependencyAwareExecutor")(...)
            canonical = _resolve_constructor_alias_rhs(func, {}, self._flow_env)
            if canonical is not None:
                self._record(
                    f"constructs {canonical}() (via dynamic getattr lookup)",
                    node.lineno,
                    is_executor=canonical in _EXECUTOR_CONSTRUCTOR_NAMES,
                )
        self.generic_visit(node)


@dataclass(frozen=True)
class _DiscoveryResult:
    call_sites: dict[tuple[str, str], set[str]]
    call_linenos: dict[tuple[str, str], int]
    executor_sites: set[tuple[str, str]]


def discover_call_and_construct_sites(root: Path, *, base: Path = REPO_ROOT) -> _DiscoveryResult:
    """Feed 1: scan every shipped ``lionagi/**/*.py`` module for qualified
    graph-execution calls, bare calls to a locally-imported kernel function,
    and executor/builder construction sites. *base* is the root paths are
    reported relative to (defaults to the repo root for the real scan; a
    test can pass a scratch directory as both *root* and *base*)."""
    call_sites: dict[tuple[str, str], set[str]] = {}
    call_linenos: dict[tuple[str, str], int] = {}
    executor_sites: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        rel = path.relative_to(base).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        kernel_names = _collect_kernel_import_bindings(tree)
        constructor_aliases, flow_env = _collect_constructor_import_bindings(tree)
        visitor = _SinkVisitor(kernel_names, constructor_aliases, flow_env)
        visitor.visit(tree)
        for qualname, reasons in visitor.hits.items():
            call_sites[(rel, qualname)] = reasons
            call_linenos[(rel, qualname)] = visitor.linenos[qualname]
        for qualname in visitor.executor_hits:
            executor_sites.add((rel, qualname))
    return _DiscoveryResult(
        call_sites=call_sites, call_linenos=call_linenos, executor_sites=executor_sites
    )


def discover_session_facade_locations() -> dict[tuple[str, str], int]:
    """Feed 3 (reflection): independently re-derive every *public* Session
    method that itself performs a kernel sink call, by parsing each
    method's own source with the same sink/import-binding logic feed 1
    uses. This does not hardcode "flow"/"flow_stream" by name — it walks
    every public method defined directly on the class, so a future public
    Session method that reaches the kernel (whether via a qualified call or
    a bare imported name, exactly like Session.flow/Session.flow_stream
    already do) is found the same way, independent of whether feed 1's
    whole-tree scan happens to also cover lionagi/session/session.py."""
    import textwrap

    from lionagi.session.session import Session

    session_path = Path(inspect.getfile(Session)).resolve().relative_to(REPO_ROOT).as_posix()
    locations: dict[tuple[str, str], int] = {}
    for name, member in vars(Session).items():
        if name.startswith("_") or not inspect.isfunction(member):
            continue
        try:
            source = textwrap.dedent(inspect.getsource(member))
            _, start_lineno = inspect.getsourcelines(member)
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):
            continue
        kernel_names = _collect_kernel_import_bindings(tree)
        constructor_aliases, flow_env = _collect_constructor_import_bindings(tree)
        visitor = _SinkVisitor(kernel_names, constructor_aliases, flow_env)
        visitor._stack = ["Session"]
        visitor.visit(tree)
        qualname = f"Session.{name}"
        if qualname in visitor.hits:
            locations[(session_path, qualname)] = start_lineno + visitor.linenos[qualname] - 1
    return locations


def format_unregistered(
    missing: set[tuple[str, str]],
    reasons: dict[tuple[str, str], set[str]],
    linenos: dict[tuple[str, str], int],
) -> str:
    lines = ["Unregistered graph-shaped entrypoints:"]
    for module_path, qualname in sorted(missing):
        why = ", ".join(sorted(reasons.get((module_path, qualname), set()))) or "reflection"
        lineno = linenos.get((module_path, qualname))
        location = f"{module_path}:{lineno}" if lineno is not None else module_path
        lines.append(f"  {location}  ({qualname})  discovered by: {why}")
    lines.append(
        "Add a GraphSurface classification and a delegation probe to "
        "tests/operations/test_graph_entrypoint_conformance.py, or classify it n/a with a reason."
    )
    return "\n".join(lines)


def format_stale(stale: set[tuple[str, str]]) -> str:
    lines = ["Stale GraphSurface registrations (no matching source found):"]
    for module_path, qualname in sorted(stale):
        lines.append(f"  {module_path}:{qualname}")
    lines.append(
        "Remove or fix the manifest entry — the code it once pointed at has moved or gone."
    )
    return "\n".join(lines)


def test_every_ast_discovered_graph_candidate_is_registered():
    """The loud two-way parity check: any new qualified .flow()/.flow_stream()/
    .run_dag() call, any new bare call to an imported kernel function, or any
    new OperationGraphBuilder/executor/Graph construction site, anywhere
    under lionagi/, must be classified in GRAPH_SURFACES — otherwise this
    fails with file:line and why."""
    discovery = discover_call_and_construct_sites(LIONAGI_ROOT)
    facade_locations = discover_session_facade_locations()
    discovered = set(discovery.call_sites) | set(facade_locations)
    registered = {s.location for s in GRAPH_SURFACES if s.location is not None}

    unregistered = discovered - registered
    stale = registered - discovered

    linenos = {**facade_locations, **discovery.call_linenos}
    assert not unregistered, format_unregistered(unregistered, discovery.call_sites, linenos)
    assert not stale, format_stale(stale)


def test_only_flow_kernels_construct_graph_executors():
    """Highest-value single assertion: no matter what a novel scheduler calls
    itself, if it constructs DependencyAwareExecutor or ReactiveExecutor
    anywhere outside operations/flow.py's own two kernel functions, this
    fails — independent of the name-based heuristic above. Covers direct
    calls, `from ... import X as Y` aliases, one-level assignment aliases
    (`Y = X`), and literal `getattr(<flow module>, "X")` dynamic lookups
    (assigned or called inline) whose receiver statically denotes
    `lionagi.operations.flow` via an import-provenance-checked binding.
    It does not perform general data-flow analysis, so
    resolution through a factory function's return value, a non-literal
    string argument, or an alias chained through more than one intermediate
    assignment remains an unanalyzable residual bypass -- see the module
    docstring."""
    discovery = discover_call_and_construct_sites(LIONAGI_ROOT)
    assert discovery.executor_sites == {
        ("lionagi/operations/flow.py", "flow"),
        ("lionagi/operations/flow.py", "flow_stream"),
    }


def test_bare_import_facade_is_discovered_by_ast_scan(tmp_path):
    """Regression for the original enumeration gap: a facade that imports the
    kernel and calls the bound name bare (`await flow(...)`, no attribute
    access) — exactly the shape Session.flow/Session.flow_stream themselves
    use — must be discoverable by feed 1, not just special-cased for Session.
    Scans a scratch directory (never lionagi/) so this proves the discovery
    function's own behavior without touching shipped source."""
    rogue = tmp_path / "rogue_facade.py"
    rogue.write_text(
        "from lionagi.operations.flow import flow as _kernel_flow\n\n"
        "class RogueFacade:\n"
        "    async def run(self, session, graph):\n"
        "        return await _kernel_flow(session=session, graph=graph)\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("rogue_facade.py", "RogueFacade.run")
    assert key in discovery.call_sites, (
        "bare call to an aliased kernel import was not discovered — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.call_sites[key] == {"calls imported _kernel_flow()"}
    assert discovery.call_linenos[key] == 5


def test_aliased_executor_import_is_discovered_by_ast_scan(tmp_path):
    """Regression for the aliased-constructor evasion: a module that imports
    the canonical executor under a different local name (`from
    lionagi.operations.flow import DependencyAwareExecutor as GraphRunner`)
    and constructs it must be recognized as an executor construction site by
    feed 1, exactly like a literal `DependencyAwareExecutor(...)` call is —
    otherwise a new production entrypoint could quietly construct the
    canonical executor outside operations/flow.py under a fresh name and
    pass both test_every_ast_discovered_graph_candidate_is_registered and
    test_only_flow_kernels_construct_graph_executors unnoticed. Scans a
    scratch directory (never lionagi/) so this proves the discovery
    function's own behavior without shipping a real violation."""
    rogue = tmp_path / "rogue_runner.py"
    rogue.write_text(
        "from lionagi.operations.flow import DependencyAwareExecutor as GraphRunner\n\n"
        "class RogueRunner:\n"
        "    async def run(self, session, graph):\n"
        "        return await GraphRunner(session, graph).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("rogue_runner.py", "RogueRunner.run")
    assert key in discovery.call_sites, (
        "constructing an aliased executor import was not discovered — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.call_sites[key] == {
        "constructs DependencyAwareExecutor() (imported as GraphRunner)"
    }
    assert discovery.executor_sites == {key}, (
        "aliased DependencyAwareExecutor construction must be flagged as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )


def test_assignment_aliased_executor_construction_is_discovered_by_ast_scan(tmp_path):
    """Regression for the value-assignment-alias evasion: a module that binds
    a plain module/class-level name to the canonical executor with a bare
    assignment (`GraphRunner = DependencyAwareExecutor`) and then constructs
    it through that name must be recognized as an executor construction site
    — this evades a scanner that only tracks `from ... import X as Y`
    aliases, since the alias here is created by ordinary assignment, not an
    import statement. Scans a scratch directory (never lionagi/) so this
    proves the discovery function's own behavior without shipping a real
    violation."""
    rogue = tmp_path / "rogue_assign_runner.py"
    rogue.write_text(
        "from lionagi.operations.flow import DependencyAwareExecutor\n\n"
        "GraphRunner = DependencyAwareExecutor\n\n\n"
        "class RogueAssignRunner:\n"
        "    async def run(self, session, graph):\n"
        "        return await GraphRunner(session, graph).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("rogue_assign_runner.py", "RogueAssignRunner.run")
    assert key in discovery.call_sites, (
        "constructing an assignment-aliased executor was not discovered — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.call_sites[key] == {
        "constructs DependencyAwareExecutor() (imported as GraphRunner)"
    }
    assert discovery.executor_sites == {key}, (
        "assignment-aliased DependencyAwareExecutor construction must be flagged as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )


def test_dynamic_getattr_executor_construction_is_discovered_by_ast_scan(tmp_path):
    """Regression for the literal dynamic-lookup evasion: a module that
    resolves the canonical executor via `getattr(importlib.import_module(...),
    "DependencyAwareExecutor")` — either assigned to a name first or called
    inline — must be recognized as an executor construction site, exactly
    like a literal `DependencyAwareExecutor(...)` call. Covers both shapes in
    one scratch directory (never lionagi/) so this proves the discovery
    function's own behavior without shipping a real violation."""
    rogue_assigned = tmp_path / "rogue_dynamic_assigned.py"
    rogue_assigned.write_text(
        "import importlib\n\n\n"
        "class RogueDynamicRunner:\n"
        "    async def run(self, session, graph):\n"
        '        cls = getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")\n'
        "        return await cls(session, graph).execute()\n"
    )
    rogue_inline = tmp_path / "rogue_dynamic_inline.py"
    rogue_inline.write_text(
        "import importlib\n\n\n"
        "class RogueInlineDynamicRunner:\n"
        "    async def run(self, session, graph):\n"
        '        return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assigned_key = ("rogue_dynamic_assigned.py", "RogueDynamicRunner.run")
    inline_key = ("rogue_dynamic_inline.py", "RogueInlineDynamicRunner.run")
    assert assigned_key in discovery.call_sites, (
        "constructing a getattr-assigned dynamic-lookup executor was not discovered — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert inline_key in discovery.call_sites, (
        "constructing an inline getattr dynamic-lookup executor was not discovered — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.call_sites[assigned_key] == {
        "constructs DependencyAwareExecutor() (imported as cls)"
    }
    assert discovery.call_sites[inline_key] == {
        "constructs DependencyAwareExecutor() (via dynamic getattr lookup)"
    }
    assert discovery.executor_sites == {assigned_key, inline_key}, (
        "both dynamic-lookup DependencyAwareExecutor constructions must be flagged as "
        f"executor sites — got: {sorted(discovery.executor_sites)}"
    )


def test_rebound_alias_is_not_misreported_as_executor_construction(tmp_path):
    """A name that once aliased the executor but was rebound to something
    else must not be reported as an executor construction: the call resolves
    to the later binding, and flagging it would let the parity gate block a
    legitimate module over a name it no longer holds."""
    benign = tmp_path / "rebound_alias.py"
    benign.write_text(
        "from lionagi.operations.flow import DependencyAwareExecutor\n\n\n"
        "class LocalRunner:\n"
        "    async def execute(self):\n"
        "        return None\n\n\n"
        "GraphRunner = DependencyAwareExecutor\n"
        "GraphRunner = LocalRunner\n\n\n"
        "class OrdinaryCaller:\n"
        "    async def run(self):\n"
        "        return await GraphRunner().execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a rebound alias must not register as an executor site — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("rebound_alias.py", "OrdinaryCaller.run") not in discovery.call_sites


def test_getattr_on_arbitrary_object_is_not_misreported_as_executor_construction(tmp_path):
    """`getattr(plugin, "DependencyAwareExecutor")` on an arbitrary receiver
    is an unrelated same-named API, not a kernel-executor construction; only
    receivers statically denoting lionagi.operations.flow (bound module name,
    dotted path, or literal import_module call) are recognized."""
    benign = tmp_path / "plugin_getattr.py"
    benign.write_text(
        "class PluginHost:\n"
        "    async def run(self, plugin, session, graph):\n"
        '        runner = getattr(plugin, "DependencyAwareExecutor")\n'
        "        await runner(session, graph).execute()\n"
        '        return await getattr(plugin, "DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "getattr on an arbitrary object must not register as an executor site — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("plugin_getattr.py", "PluginHost.run") not in discovery.call_sites


def test_shadowed_lionagi_name_dotted_getattr_is_not_misreported(tmp_path):
    """A local name `lionagi` bound to something else means the dotted
    expression `lionagi.operations.flow` no longer denotes the real package;
    without an import-bound `lionagi` root, the getattr receiver must not be
    treated as the flow module."""
    benign = tmp_path / "shadowed_root.py"
    benign.write_text(
        "lionagi = plugin\n"
        "async def run(session, graph):\n"
        '    return await getattr(lionagi.operations.flow, "DependencyAwareExecutor")(\n'
        "        session, graph\n"
        "    ).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a shadowed dotted receiver must not register as an executor site — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("shadowed_root.py", "run") not in discovery.call_sites


def test_arbitrary_import_module_named_callee_is_not_misreported(tmp_path):
    """A method merely *named* `import_module` on an arbitrary object is not
    importlib's; without an import-bound `importlib` (or `from importlib
    import import_module`) provenance, the call must not be treated as
    producing the flow module."""
    benign = tmp_path / "plugin_import_module.py"
    benign.write_text(
        "async def run(plugin, session, graph):\n"
        '    return await getattr(plugin.import_module("lionagi.operations.flow"),\n'
        '                         "DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "an arbitrary import_module-named callee must not register as an executor site — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("plugin_import_module.py", "run") not in discovery.call_sites


def test_reimport_after_unrelated_rebinding_restores_executor_discovery(tmp_path):
    """Regression for the ordering false-negative: a genuine later
    `import importlib` must restore the provenance an intervening unrelated
    rebinding (`importlib = plugin`) discarded, because import bindings and
    assignment bindings are now replayed as one combined source-ordered pass
    instead of two independent passes (all imports applied up front, then
    assignments applied after) — under the old two-pass model this
    construction site was silently missed even though the re-import comes
    after the rebind in source."""
    rogue = tmp_path / "reimport_restores.py"
    rogue.write_text(
        "plugin = object()\n"
        "import importlib\n"
        "importlib = plugin\n"
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("reimport_restores.py", "run")
    assert key in discovery.call_sites, (
        "a later re-import restoring provenance after an intervening unrelated rebind was "
        f"not discovered — found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the re-import-restored dynamic lookup must be flagged as an executor site — got: "
        f"{sorted(discovery.executor_sites)}"
    )


def test_reimport_of_lionagi_root_after_rebinding_restores_executor_discovery(tmp_path):
    """Same ordering false-negative for the `lionagi` package root: a
    rebinding assignment (`lionagi = plugin`) followed by a later genuine
    `import lionagi` must restore the dotted `lionagi.operations.flow`
    receiver's provenance, since the re-import is the last binding event for
    that name in source order."""
    rogue = tmp_path / "reimport_lionagi_root.py"
    rogue.write_text(
        "plugin = object()\n"
        "lionagi = plugin\n"
        "import lionagi\n\n\n"
        "async def run(session, graph):\n"
        '    return await getattr(lionagi.operations.flow, "DependencyAwareExecutor")(\n'
        "        session, graph\n"
        "    ).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("reimport_lionagi_root.py", "run")
    assert key in discovery.call_sites, (
        "a later re-import of the lionagi root restoring provenance was not discovered — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the re-import-restored dotted lionagi receiver must be flagged as an executor "
        f"site — got: {sorted(discovery.executor_sites)}"
    )


def test_function_parameter_shadowing_importlib_import_is_not_misreported(tmp_path):
    """Regression for the lexical-scoping false-positive: a module-level
    `import importlib` must not be trusted inside a function whose own
    PARAMETER is named `importlib` — the parameter shadows the module-level
    binding for the whole function body, so the receiver no longer denotes
    the real importlib module there."""
    benign = tmp_path / "param_shadows_importlib.py"
    benign.write_text(
        "import importlib\n\n\n"
        "async def run(importlib, session, graph):\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a parameter shadowing the module-level importlib import must not register as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )
    assert ("param_shadows_importlib.py", "run") not in discovery.call_sites


def test_function_parameter_shadowing_lionagi_import_is_not_misreported(tmp_path):
    """Same lexical-scoping false-positive for the `lionagi` package root: a
    function parameter named `lionagi` shadows the module-level `import
    lionagi` for the whole function body, so a dotted
    `lionagi.operations.flow` receiver inside it must not be trusted."""
    benign = tmp_path / "param_shadows_lionagi.py"
    benign.write_text(
        "import lionagi\n\n\n"
        "async def run(lionagi, session, graph):\n"
        '    return await getattr(lionagi.operations.flow, "DependencyAwareExecutor")(\n'
        "        session, graph\n"
        "    ).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a parameter shadowing the module-level lionagi import must not register as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )
    assert ("param_shadows_lionagi.py", "run") not in discovery.call_sites


def test_conditional_rebind_and_conditional_reimport_is_not_misreported(tmp_path):
    """Regression for the conditional-provenance false positive: an
    unconditional `import importlib` followed by an if/else where the `if`
    branch rebinds `importlib` to an arbitrary object and the `else` branch
    re-imports it must NOT register a site. The checker cannot know which
    branch runs, so the conservative/false-positive-safe reading is that the
    rebind might have happened (clears provenance) while the re-import might
    never run (does not restore it) -- net: no provenance, no site. Under
    the old flat whole-module source-ordered replay, the `else`-branch
    re-import (later in source than the rebind) wrongly restored provenance
    regardless of which branch is live."""
    rogue = tmp_path / "conditional_if_else.py"
    rogue.write_text(
        "import importlib\n"
        "if True:\n"
        "    importlib = object()\n"
        "else:\n"
        "    import importlib\n\n\n"
        "async def run(session, graph):\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "an if/else rebind-then-conditionally-reimport must not register as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )
    assert ("conditional_if_else.py", "run") not in discovery.call_sites


def test_conditional_rebind_and_conditional_reimport_in_try_except_is_not_misreported(tmp_path):
    """Same conditional-provenance false positive, `try`/`except` shape:
    the `try` body rebinds `importlib` and the `except` handler re-imports
    it -- neither is guaranteed to run in the reported order, so this must
    not register a site either."""
    rogue = tmp_path / "conditional_try_except.py"
    rogue.write_text(
        "import importlib\n"
        "try:\n"
        "    importlib = object()\n"
        "except Exception:\n"
        "    import importlib\n\n\n"
        "async def run(session, graph):\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a try/except rebind-then-conditionally-reimport must not register as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )
    assert ("conditional_try_except.py", "run") not in discovery.call_sites


def test_unconditional_import_after_conditional_block_still_yields_a_site(tmp_path):
    """Companion to the two conditional false-positive regressions above: a
    conditional rebind inside an `if` block must not poison an unconditional,
    real import that comes AFTER the block ends at the same (module) scope --
    provenance still resolves normally once control flow returns to an
    unconditional statement."""
    rogue = tmp_path / "conditional_then_unconditional.py"
    rogue.write_text(
        "import importlib\n"
        "if True:\n"
        "    importlib = object()\n"
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("conditional_then_unconditional.py", "run")
    assert key in discovery.call_sites, (
        "an unconditional import after a conditional rebind block was not discovered — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the unconditional post-block import must be flagged as an executor site — got: "
        f"{sorted(discovery.executor_sites)}"
    )


def test_in_body_reimport_restores_importlib_provenance_masked_by_parameter(tmp_path):
    """Regression for the parameter-shadow false negative: a function whose
    parameter is named `importlib` (masking the module-level import for the
    duration of the function body, per
    test_function_parameter_shadowing_importlib_import_is_not_misreported)
    but which then performs its OWN `import importlib` inside the body must
    have that re-import recognized -- the in-body import is a genuine,
    unconditional statement of the function's own scope and really does
    rebind the name to the real module at runtime."""
    rogue = tmp_path / "reimport_inside_shadowed_scope.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "async def run(importlib, session, graph):\n"
        "    import importlib\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("reimport_inside_shadowed_scope.py", "run")
    assert key in discovery.call_sites, (
        "a genuine in-body reimport restoring parameter-shadowed provenance was not "
        f"discovered — found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the in-body-reimport-restored dynamic lookup must be flagged as an executor "
        f"site — got: {sorted(discovery.executor_sites)}"
    )


def test_in_body_reimport_restores_lionagi_root_provenance_masked_by_parameter(tmp_path):
    """Same parameter-shadow false negative for the `lionagi` package root,
    dotted-attribute shape: a function parameter named `lionagi` masks the
    module-level `import lionagi`, but an in-body `import lionagi` genuinely
    restores it for a dotted `lionagi.operations.flow` receiver."""
    rogue = tmp_path / "reimport_lionagi_inside_shadowed_scope.py"
    rogue.write_text(
        "import lionagi\n\n\n"
        "async def run(lionagi, session, graph):\n"
        "    import lionagi\n"
        '    return await getattr(lionagi.operations.flow, "DependencyAwareExecutor")(\n'
        "        session, graph\n"
        "    ).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("reimport_lionagi_inside_shadowed_scope.py", "run")
    assert key in discovery.call_sites, (
        "a genuine in-body reimport restoring parameter-shadowed lionagi-root provenance "
        f"was not discovered — found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the in-body-reimport-restored dotted lionagi receiver must be flagged as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )


def test_session_flow_and_flow_stream_are_discovered_via_bare_import_not_hardcoding(tmp_path):
    """The Session facade methods are no longer special-cased by literal name
    (the original discover_session_facade_locations hardcoded "flow" and
    "flow_stream") — they're found generically because they happen to match
    the same bare-imported-kernel-call shape every other facade is checked
    against. A method that does NOT call the kernel must not be flagged."""
    facades = discover_session_facade_locations()
    session_rel = Path("lionagi/session/session.py").as_posix()
    assert (session_rel, "Session.flow") in facades
    assert (session_rel, "Session.flow_stream") in facades
    assert (session_rel, "Session.include_branches") not in facades


def _test_function_exists(node_id: str) -> bool:
    """Resolve a "path/to/test_module.py::test_name" pytest node id against
    real source without importing the module (so an optional-extra-gated
    module, e.g. one that needs fastapi/aiosqlite, can still be validated
    even when that extra isn't installed here). Returns False for a
    nonexistent file or a name that isn't a top-level def/async def in it —
    exactly the shape of evidence a free-form, unchecked string can't catch."""
    module_path_str, sep, test_name = node_id.partition("::")
    if not sep or not test_name:
        return False
    module_path = REPO_ROOT / module_path_str
    if not module_path.is_file():
        return False
    tree = ast.parse(module_path.read_text(), filename=module_path_str)
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == test_name
        for node in tree.body
    )


def test_every_required_persistence_surface_has_named_evidence():
    """persistence_evidence must be both present and resolvable: a nonexistent
    file (or a typo'd test name) fails loudly instead of reading as coverage —
    the exact shape of the li-engine-run-planning gap this closes."""
    required = [s for s in GRAPH_SURFACES if s.persistence == "required"]
    assert required, "expected at least one persistence=required surface"
    for surface in required:
        assert surface.persistence_evidence, (
            f"{surface.key} claims persistence=required with no evidence test id"
        )
        assert _test_function_exists(surface.persistence_evidence), (
            f"{surface.key}.persistence_evidence={surface.persistence_evidence!r} does not "
            "resolve to a real test function — fix the reference or write the test"
        )


def test_every_delegation_target_has_a_named_test():
    """Every surface that names an expected_target must also name the exact
    test that asserts it — expected_target alone is unread metadata that a
    new manifest row can carry without ever being enforced."""
    targeted = [s for s in GRAPH_SURFACES if s.expected_target is not None]
    assert targeted, "expected at least one surface with an expected_target"
    for surface in targeted:
        assert surface.delegation_test, (
            f"{surface.key} declares expected_target={surface.expected_target!r} but has "
            "no delegation_test id proving it — add a probe or exempt it with a reason"
        )


def test_every_delegation_test_id_resolves_to_a_real_test_function():
    for surface in GRAPH_SURFACES:
        if surface.delegation_test is None:
            continue
        assert _test_function_exists(surface.delegation_test), (
            f"{surface.key}.delegation_test={surface.delegation_test!r} does not resolve "
            "to a real test function — fix the reference or the test"
        )


_GENERIC_TARGET_WORDS = {"the", "and", "for"}


def _derive_target_tokens(expected_target: str) -> set[str]:
    """Cheap, deliberately weak structural signal: identifier-like tokens
    pulled out of a manifest row's ``expected_target`` (dotted symbols like
    ``Session.flow``, or free-form argv-shape text like ``["o", "flow",
    ...]``). Used only to sanity-check that a cited ``delegation_test``'s own
    source at least mentions something related to what the row claims it
    proves — not a replacement for reading the test."""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expected_target)
    return {t for t in tokens if len(t) >= 3 and t.lower() not in _GENERIC_TARGET_WORDS}


def test_every_delegation_test_source_mentions_its_expected_target():
    """Weak structural companion to
    test_every_delegation_test_id_resolves_to_a_real_test_function: a
    resolvable test function name alone proves nothing about what the test
    body actually asserts. That gap is exactly how the studio-engine-node
    row's prior ``expected_target="EngineRun.run_dag"`` went unnoticed while
    citing a real, passing test that only ever exercises ``engine.run`` — a
    different relationship entirely. This does not read intent or assert
    call counts; it only fails loudly if a cited test's source text mentions
    NONE of the identifier-like tokens drawn from expected_target, which
    would catch a row citing a wildly unrelated test (wrong class/module/
    symbol). A passing result here is necessary, not sufficient."""
    for surface in GRAPH_SURFACES:
        if surface.expected_target is None or surface.delegation_test is None:
            continue
        tokens = _derive_target_tokens(surface.expected_target)
        if not tokens:
            continue
        module_path_str, _, test_name = surface.delegation_test.partition("::")
        module_path = REPO_ROOT / module_path_str
        source = module_path.read_text()
        tree = ast.parse(source, filename=module_path_str)
        func_node = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == test_name
            ),
            None,
        )
        assert func_node is not None, (
            f"{surface.key}.delegation_test={surface.delegation_test!r} did not resolve "
            "while deriving its source — should have failed the resolution test first"
        )
        func_source = ast.get_source_segment(source, func_node) or ""
        assert any(tok.lower() in func_source.lower() for tok in tokens), (
            f"{surface.key}.delegation_test={surface.delegation_test!r} source mentions none "
            f"of {sorted(tokens)} derived from expected_target={surface.expected_target!r} — "
            "the cited test may not actually assert this relationship"
        )


def test_every_surface_has_a_reason():
    for surface in GRAPH_SURFACES:
        assert surface.reason, f"{surface.key} has no classification reason"


def test_manifest_keys_and_symbols_are_unique():
    keys = [s.key for s in GRAPH_SURFACES]
    assert len(keys) == len(set(keys)), "duplicate GraphSurface.key values"
    symbols = [s.symbol for s in GRAPH_SURFACES]
    assert len(symbols) == len(set(symbols)), "duplicate GraphSurface.symbol values"


# ---------------------------------------------------------------------------
# 3. Registry classification (Feed 2)
# ---------------------------------------------------------------------------


def test_orchestrate_subcommands_are_exactly_fanout_flow_and_non_graph_ctl():
    import argparse

    from lionagi.cli.orchestrate import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li", add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    registered = add_orchestrate_subparser(subparsers)

    assert set(registered) == {"fanout", "flow", "ctl"}, (
        "a new `li o <subcommand>` was registered without a graph/non-graph decision here"
    )


@pytest.mark.asyncio
async def test_engine_run_planning_kind_uses_real_planning_engine_and_persists_via_statedb(
    monkeypatch, tmp_path
):
    """li-engine-run-planning: `_do_engine_run(kind="planning")` must resolve
    and drive the REAL PlanningEngine class (not merely whatever
    `_import_engine_class` happens to return) and, with persistence enabled,
    leave a completed row behind in a real StateDB — replacing the prior
    free-form "tests/cli/engine" evidence string, which named a path that
    does not exist and was never checked. Only the LLM-touching
    _plan/_synthesize stages and the run_dag hop (already pinned
    identity-preserving by test_planning_engine_run_delegates_to_
    engine_run_run_dag above) are stubbed; class resolution, Engine.run,
    PlanningEngine._run, and StateDB persistence all run for real."""
    from types import SimpleNamespace as _SimpleNamespace
    from uuid import UUID

    import lionagi.cli._logging as log_mod
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod
    from lionagi.casts.emission import TaskAssignment
    from lionagi.engines.engine import EngineRun
    from lionagi.engines.planning import PlanningEngine
    from lionagi.state.db import StateDB

    monkeypatch.setattr(log_mod, "progress", lambda *a, **kw: None)
    monkeypatch.setattr(log_mod, "warn", lambda *a, **kw: None)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(db_mod, "settings", _SimpleNamespace(LIONAGI_STATE_DB_URL=None))

    fixed_run_id = UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(engine_mod.uuid, "uuid4", lambda: fixed_run_id)

    resolved_classes: list[type] = []
    real_import = engine_mod._import_engine_class

    def _spying_import(module, name):
        cls = real_import(module, name)
        resolved_classes.append(cls)
        return cls

    monkeypatch.setattr(engine_mod, "_import_engine_class", _spying_import)

    async def fake_plan(self, run, prompt, max_ops=0):
        return [TaskAssignment(task="x", assignee="researcher")]

    async def fake_synthesize(self, run, prompt, assignments, node_ids, result):
        return "synthesis complete"

    run_dag_spy = AsyncMock(return_value={"operation_results": {}, "completed_operations": []})
    monkeypatch.setattr(PlanningEngine, "_plan", fake_plan)
    monkeypatch.setattr(PlanningEngine, "_synthesize", fake_synthesize)
    monkeypatch.setattr(EngineRun, "run_dag", run_dag_spy)

    async def fake_spawn_roles(session, specs, *, spawners=()):
        return {}

    monkeypatch.setattr("lionagi.engines.planning.spawn_roles", fake_spawn_roles)

    def fake_build_dag_graph(session, assignments, roles):
        from lionagi.protocols.types import Graph

        return Graph(), [None for _ in assignments]

    monkeypatch.setattr("lionagi.engines.planning.build_dag_graph", fake_build_dag_graph)

    args = argparse.Namespace(
        command="engine",
        engine_command="run",
        kind="planning",
        spec="Build something",
        test_cmd=None,
        export_dir=None,
        model=None,
        max_depth=None,
        max_agents=None,
        session_id=None,
        no_persist=False,
    )
    rc = await engine_mod._do_engine_run(args)

    assert rc == 0
    assert resolved_classes == [PlanningEngine], (
        "_do_engine_run(kind='planning') must resolve the real PlanningEngine class"
    )
    run_dag_spy.assert_called_once()

    db = StateDB(tmp_path / "state.db")
    await db.open()
    try:
        row = await db.get_engine_run(fixed_run_id.hex)
    finally:
        await db.close()
    assert row is not None, "engine run row was not persisted to StateDB"
    assert row["kind"] == "planning"
    assert row["status"] == "completed"


def test_engine_kind_planning_is_the_only_registered_graph_kind():
    from lionagi.cli.engine import _KIND_META

    assert _KIND_META["planning"]["cls_path"] == ("lionagi.engines", "PlanningEngine")

    non_graph_kinds = set(_KIND_META) - {"planning"}
    assert non_graph_kinds == {"research", "review", "coding", "hypothesis"}, (
        "a new `li engine run <kind>` was registered without a graph/non-graph decision here"
    )
    for kind in non_graph_kinds:
        _module, cls_name = _KIND_META[kind]["cls_path"]
        assert cls_name != "PlanningEngine"


# ---------------------------------------------------------------------------
# 4. Facade / kernel probes
# ---------------------------------------------------------------------------


def _minimal_session_with_branch():
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    return session, branch


@pytest.mark.asyncio
async def test_session_flow_delegates_to_operations_flow_kernel_with_same_identity():
    import sys

    flow_module = sys.modules["lionagi.operations.flow"]
    from lionagi.operations.builder import OperationGraphBuilder

    session, branch = _minimal_session_with_branch()
    builder = OperationGraphBuilder()
    builder.add_operation("noop")
    graph = builder.get_graph()

    spy = AsyncMock(return_value={"completed_operations": []})
    with mock.patch.object(flow_module, "flow", spy):
        result = await session.flow(graph)

    spy.assert_called_once()
    assert spy.call_args.kwargs["session"] is session
    assert spy.call_args.kwargs["graph"] is graph
    assert result == {"completed_operations": []}


@pytest.mark.asyncio
async def test_session_flow_stream_delegates_to_streaming_kernel_without_calling_ordinary_flow():
    """The sanctioned exception: flow_stream must reach the streaming kernel,
    and must NOT be required to also call ordinary Session.flow/operations.flow.flow
    — asserting that would be a false positive against the streaming path."""
    import sys

    flow_module = sys.modules["lionagi.operations.flow"]
    from lionagi.operations.builder import OperationGraphBuilder

    session, branch = _minimal_session_with_branch()
    builder = OperationGraphBuilder()
    builder.add_operation("noop")
    graph = builder.get_graph()

    captured: dict = {}

    async def fake_flow_stream(*, session, graph, **kwargs):
        captured["session"] = session
        captured["graph"] = graph
        yield "event-1"

    ordinary_flow_spy = AsyncMock(
        side_effect=AssertionError("flow_stream must not call ordinary flow")
    )

    with (
        mock.patch.object(flow_module, "flow_stream", fake_flow_stream),
        mock.patch.object(flow_module, "flow", ordinary_flow_spy),
    ):
        events = [e async for e in session.flow_stream(graph)]

    assert events == ["event-1"]
    assert captured["session"] is session
    assert captured["graph"] is graph
    ordinary_flow_spy.assert_not_called()


@pytest.mark.asyncio
async def test_flow_kernel_executes_via_dependency_aware_executor_when_not_reactive():
    from lionagi.operations.flow import flow

    async def work(**kw):
        return "ok"

    session, branch = _minimal_session_with_branch()
    session.register_operation("work", work)

    from lionagi.operations.builder import OperationGraphBuilder

    builder = OperationGraphBuilder()
    builder.add_operation("work")
    graph = builder.get_graph()

    result = await flow(session, graph, branch=branch, reactive=False)

    assert result["completed_operations"]
    assert "operation_results" in result
    assert "skipped_operations" in result


# ---------------------------------------------------------------------------
# 5. Library / adapter probes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestration_fanout_submits_built_graph_through_injected_session_flow():
    from uuid import uuid4

    from lionagi.casts.emission import TaskAssignment
    from lionagi.orchestration.patterns import fanout
    from lionagi.session.branch import Branch

    class _FakeFanoutSession:
        """Duck-typed session — build_fanout_graph only needs .id/.include_branches,
        and fanout() only needs .flow; a real (pydantic) Session forbids instance
        attribute overrides, so this is the lightest correct probe seam."""

        def __init__(self) -> None:
            self.id = uuid4()
            self.branches: list = []
            self.flow_calls: list = []

        def include_branches(self, branch) -> None:
            self.branches.append(branch)

        async def flow(self, graph, **kwargs):
            self.flow_calls.append(graph)
            return {"operation_results": {}, "completed_operations": []}

    session = _FakeFanoutSession()
    roles = {"researcher": Branch(name="researcher")}
    assignments = [TaskAssignment(task="dig in", assignee="researcher")]

    result = await fanout(session, assignments, roles)

    assert len(session.flow_calls) == 1
    assert result == {"operation_results": {}, "completed_operations": []}


@pytest.mark.asyncio
async def test_planning_engine_run_delegates_to_engine_run_run_dag():
    from lionagi.casts.emission import TaskAssignment
    from lionagi.engines.engine import EngineRun
    from lionagi.engines.planning import PlanningEngine

    eng = PlanningEngine(reactive=False)
    run = eng.new_run()

    assignments = [TaskAssignment(task="x", assignee="researcher")]
    sentinel_graph = object()

    async def fake_plan(_run, prompt, max_ops):
        return assignments

    async def fake_spawn_roles(session, specs, *, spawners=()):
        return {"researcher": object()}

    def fake_build_dag_graph(session, asg, roles):
        return (sentinel_graph, ["n1"])

    async def fake_synth(_run, prompt, asg, node_ids, result):
        return "FINAL"

    eng._plan = fake_plan
    eng._synthesize = fake_synth

    run_dag_spy = AsyncMock(return_value={"operation_results": {"n1": "ok"}})
    with (
        mock.patch("lionagi.engines.planning.spawn_roles", fake_spawn_roles),
        mock.patch("lionagi.engines.planning.build_dag_graph", fake_build_dag_graph),
        mock.patch.object(EngineRun, "run_dag", run_dag_spy),
    ):
        out = await eng._run(run, "task")

    assert out == "FINAL"
    run_dag_spy.assert_called_once()
    passed_graph = (
        run_dag_spy.call_args.args[0]
        if run_dag_spy.call_args.args
        else run_dag_spy.call_args.kwargs.get("graph")
    )
    assert passed_graph is sentinel_graph


@pytest.mark.asyncio
async def test_run_fanout_inner_calls_env_session_flow_with_builder_graph(tmp_path):
    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate import fanout as fanout_mod
    from tests.cli.orchestrate.test_flow_phases import _FakeBranch, _make_env

    env = _make_env(tmp_path)
    env.exchange = None

    assignments = [TaskAssignment(task="do it", assignee="researcher")]

    async def fake_plan(*a, **kw):
        return assignments

    def fake_available_roles():
        return ["researcher"]

    async def fake_build_worker_branch(env, *, agent_id, role, model_override=None, **kw):
        return _FakeBranch(role), "codex/gpt-5.5", None, False

    def fake_finalize(*a, **kw):
        return ([], "")

    env.session.flow = AsyncMock(return_value={"operation_results": {}})

    with (
        mock.patch.object(fanout_mod, "plan", fake_plan),
        mock.patch.object(fanout_mod, "available_roles", fake_available_roles),
        mock.patch.object(fanout_mod, "build_worker_branch", fake_build_worker_branch),
        mock.patch.object(fanout_mod, "finalize_orchestration", fake_finalize),
    ):
        await fanout_mod._run_fanout_inner("codex/gpt-5.5", "do the batch", env=env, num_workers=1)

    env.session.flow.assert_called_once()
    called_graph = env.session.flow.call_args.args[0]
    assert called_graph.nodes == env.builder._nodes
    assert len(called_graph.nodes) == 1


def _persistence_seam_env(tmp_path):
    """Real Session/Branch OrchestrationEnv for driving a CLI wrapper's own
    start_live_persist call site for real, the same shape
    tests/cli/orchestrate/test_flow_terminal_notify.py's `_make_env` uses —
    unlike the generic tests/cli/orchestrate/test_live_persist.py fixture,
    the tests below never call start_live_persist directly."""
    from types import SimpleNamespace

    from lionagi import Branch, Session
    from lionagi.cli.orchestrate._orchestration import OrchestrationEnv

    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    run = SimpleNamespace(run_id="run-test-1", artifact_root=tmp_path / "artifacts")
    env = OrchestrationEnv(
        run=run,
        session=session,
        orc_branch=orc_branch,
        builder=mock.MagicMock(),
        orc_profile=None,
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
    return session, env


@pytest.mark.asyncio
async def test_run_fanout_persists_session_via_start_live_persist(tmp_path, monkeypatch):
    """cli-o-fanout persistence: drives the REAL `_run_fanout` wrapper (only
    `setup_orchestration` and the heavy `_run_fanout_inner` execution are
    stubbed — the wrapper's own `start_live_persist` call at
    lionagi/cli/orchestrate/fanout.py:93 is NOT) and asserts a real StateDB
    session row exists afterward. This is the seam the generic
    tests/cli/orchestrate/test_live_persist.py::
    test_start_creates_session_and_registers_hook_on_orc_branch test cannot
    cover: that test calls start_live_persist directly and never touches
    `_run_fanout` at all, so deleting fanout.py's call site would leave it
    green. Deleting that call site here leaves no StateDB row and fails."""
    from lionagi.cli.orchestrate.fanout import _run_fanout
    from lionagi.state.db import StateDB

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    session, env = _persistence_seam_env(tmp_path)

    with (
        mock.patch(
            "lionagi.cli.orchestrate.fanout.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        mock.patch(
            "lionagi.cli.orchestrate.fanout._run_fanout_inner",
            AsyncMock(return_value="ok result"),
        ),
    ):
        result, status = await _run_fanout("claude", "do the batch")

    assert result == "ok result"
    assert status == "completed"

    async with StateDB() as db:
        row = await db.get_session(str(session.id))
    assert row is not None, (
        "no StateDB session row after _run_fanout — its start_live_persist call site is gone"
    )
    assert row["invocation_kind"] == "fanout"


@pytest.mark.asyncio
async def test_run_flow_persists_session_via_start_live_persist(tmp_path, monkeypatch):
    """cli-o-flow-exec/cli-o-flow-synth persistence: drives the REAL
    `_run_flow` wrapper (only `setup_orchestration` and the heavy
    `_run_flow_inner` execution are stubbed — the wrapper's own
    `start_live_persist` call at lionagi/cli/orchestrate/flow.py:1477 is NOT)
    and asserts a real StateDB session row exists afterward, closing the
    same gap as test_run_fanout_persists_session_via_start_live_persist for
    the flow CLI. HOME is isolated so the finally block's
    fire_terminal_notify never picks up a real machine's notify settings."""
    from lionagi.cli.orchestrate.flow import _run_flow
    from lionagi.state.db import StateDB

    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    session, env = _persistence_seam_env(tmp_path)

    with (
        mock.patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        mock.patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok result"),
        ),
    ):
        result, status = await _run_flow("claude", "do the thing")

    assert result == "ok result"
    assert status == "completed"

    async with StateDB() as db:
        row = await db.get_session(str(session.id))
    assert row is not None, (
        "no StateDB session row after _run_flow — its start_live_persist call site is gone"
    )
    assert row["invocation_kind"] == "flow"


@pytest.mark.asyncio
async def test_execute_dag_delegates_to_planning_engine_run_dag(tmp_path):
    """cli-o-flow: the run_dag -> Session.flow hop itself is proven by
    engine-run-dag; this only pins that _execute_dag reaches run_dag once
    with the built graph — reusing the same fake-env/PlanningEngine.new_run
    seam tests/cli/orchestrate/test_flow_phases.py already established."""
    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine
    from tests.cli.orchestrate.test_flow_phases import _FakeBranch, _make_env

    env = _make_env(tmp_path)
    assignments = [TaskAssignment(task="x", assignee="researcher")]
    env.session.include_branches(_FakeBranch("researcher"))

    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    run_dag_spy = AsyncMock(
        return_value={"operation_results": {"node-0": "ok"}, "spawned_operations": 0}
    )
    fake_engine_run = mock.MagicMock()
    fake_engine_run.run_dag = run_dag_spy

    with mock.patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    run_dag_spy.assert_called_once()
    passed_graph = run_dag_spy.call_args.args[0]
    assert passed_graph.nodes == env.builder._nodes


@pytest.mark.asyncio
async def test_synthesize_calls_env_session_flow_with_builder_graph(tmp_path):
    """cli-o-flow-synth: `_synthesize` submits the final synthesis op directly
    through `env.session.flow`, a distinct call site from the run_dag bridge
    cli-o-flow-exec uses — it needs its own identity-preserving probe rather
    than relying on expected_target being read anywhere."""
    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _ExecResult, _PlanResult, _synthesize
    from tests.cli.orchestrate.test_flow_phases import _FakeBranch, _make_env

    env = _make_env(tmp_path)
    env.session.include_branches(_FakeBranch("researcher"))

    assignments = [TaskAssignment(task="x", assignee="researcher")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["researcher"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=set(),
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "researcher",
                "agent_id": "researcher",
                "name": "researcher",
                "response": "findings",
            }
        ],
        n_spawned=0,
        t_exec_elapsed=1.0,
    )

    flow_spy = AsyncMock(return_value={"operation_results": {}, "completed_operations": []})
    env.session.flow = flow_spy

    await _synthesize(
        env,
        "task",
        plan_result,
        dag_state,
        exec_result,
        synthesis_model=None,
        model_spec="codex/gpt-5.5",
    )

    flow_spy.assert_called_once()
    passed_graph = flow_spy.call_args.args[0]
    assert passed_graph.nodes == env.builder._nodes
    assert len(passed_graph.nodes) == 1


@pytest.mark.asyncio
async def test_resume_flow_calls_run_flow_with_resolved_checkpoint(tmp_path):
    import json as _json

    import lionagi.cli.orchestrate._checkpoint as ckmod
    from lionagi.cli.orchestrate import flow as flow_mod

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "20260703T000000-abc123"
    run_dir.mkdir(parents=True)
    checkpoint = {
        "version": 1,
        "session_id": "s1",
        "prompt": "resume me",
        "plan": [],
        "flow_context": {},
        "ops": {},
        "spawned": [],
        "config": {},
    }
    (run_dir / "checkpoint.json").write_text(_json.dumps(checkpoint))

    calls: list[dict] = []

    async def fake_run_flow(**kwargs):
        calls.append(kwargs)
        return "output", "completed"

    with (
        mock.patch.object(ckmod, "RUNS_ROOT", runs_root),
        mock.patch.object(flow_mod, "_run_flow", fake_run_flow),
    ):
        output, status = await flow_mod._resume_flow("20260703T000000-abc123")

    assert status == "completed"
    assert output == "output"
    assert len(calls) == 1
    assert calls[0]["resume_checkpoint"]["session_id"] == "s1"
    assert calls[0]["prompt"] == "resume me"


def test_play_shortcut_rewrites_to_registered_o_flow_subcommand():
    from lionagi.cli.main import _handle_play_shortcut

    result = _handle_play_shortcut(["play", "myplaybook", "do the thing"])

    assert result[:4] == ["o", "flow", "-p", "myplaybook"]


def test_scheduler_build_argv_only_dispatches_registered_o_subcommands():
    from lionagi.studio.scheduler.subprocess import build_argv

    flow_argv, _tmp = build_argv(
        {"action_kind": "flow", "action_model": "codex/gpt-5.5", "action_prompt": "do it"}, {}
    )
    assert flow_argv[3:5] == ["o", "flow"]

    fanout_argv, _tmp = build_argv(
        {"action_kind": "fanout", "action_model": "codex/gpt-5.5", "action_prompt": "do it"}, {}
    )
    assert fanout_argv[3:5] == ["o", "fanout"]

    play_argv, _tmp = build_argv({"action_kind": "play", "action_playbook": "myplaybook"}, {})
    assert play_argv[3] == "play"

    import os

    flow_yaml_argv, flow_yaml_tmp = build_argv(
        {"action_kind": "flow_yaml", "action_flow_yaml": "prompt: hi\n"}, {}
    )
    try:
        assert flow_yaml_argv[3:5] == ["o", "flow"]
        assert "-f" in flow_yaml_argv
    finally:
        if flow_yaml_tmp:
            os.unlink(flow_yaml_tmp)


# ---------------------------------------------------------------------------
# 6. Studio probes (skip cleanly when the studio extra is not installed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_studio_workflow_route_delegates_to_run_workflow_def():
    pytest.importorskip("fastapi", reason="studio extra not installed")
    import lionagi.studio.services.workflow_run as workflow_run_mod
    from lionagi.studio.services.workflow_defs import RunWorkflowDefRequest, run_workflow_def_route

    spy = AsyncMock(return_value={"run_id": "abc", "status": "completed"})
    with mock.patch.object(workflow_run_mod, "run_workflow_def", spy):
        result = await run_workflow_def_route("def-1", RunWorkflowDefRequest(inputs={"topic": "x"}))

    spy.assert_called_once()
    assert spy.call_args.args[0] == "def-1"
    assert result == {"run_id": "abc", "status": "completed"}


@pytest.mark.asyncio
async def test_run_workflow_def_delegates_to_session_flow_with_progress_wrapper(
    tmp_path, monkeypatch
):
    pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
    pytest.importorskip("fastapi", reason="studio extra not installed")

    import lionagi.state.db as db_mod
    import lionagi.studio.services.engine_defs as engine_defs_svc
    import lionagi.studio.services.sessions as sessions_svc
    import lionagi.studio.services.workflow_defs as wf_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(wf_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(engine_defs_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_svc, "_DB", str(db_path))

    import lionagi.cli.engine as cli_engine
    from tests.apps_studio_server.test_workflow_run import _FakeEngine, _mock_chat_branch, _spec

    monkeypatch.setitem(
        cli_engine._KIND_META,
        "research",
        {
            **cli_engine._KIND_META["research"],
            "cls_path": ("tests.apps_studio_server.test_workflow_run", "_FakeEngine"),
        },
    )
    _FakeEngine.calls = []

    engine_def = await engine_defs_svc.create_engine_def({"name": "conf-eng", "kind": "research"})
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    created = await wf_svc.create_workflow_def({"name": "conf-flow", "spec_json": spec})

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    session = Session(default_branch=_mock_chat_branch())

    calls: list[tuple] = []
    original_flow = Session.flow

    async def _spy_flow(self, *args, **kwargs):
        calls.append((self, args, kwargs))
        return await original_flow(self, *args, **kwargs)

    with mock.patch.object(Session, "flow", _spy_flow):
        result = await run_workflow_def(created["id"], {"topic": "GQA"}, _session=session)

    assert result["status"] == "completed"
    assert len(calls) == 1
    called_self, _called_args, called_kwargs = calls[0]
    assert called_self is session
    assert called_kwargs.get("on_progress") is not None


@pytest.mark.asyncio
async def test_compile_workflow_def_never_calls_session_flow(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
    pytest.importorskip("fastapi", reason="studio extra not installed")

    import lionagi.state.db as db_mod
    import lionagi.studio.services.engine_defs as engine_defs_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(engine_defs_svc, "DEFAULT_DB_PATH", db_path)

    from tests.apps_studio_server.test_workflow_run import _spec

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "compile-guard", "kind": "research"}
    )
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_compile import compile_workflow_def

    assert inspect.iscoroutinefunction(compile_workflow_def)

    async def _forbidden(self, *a, **kw):
        raise AssertionError("compile_workflow_def must not execute the graph it compiles")

    async def _resolve_engine_def(ref: str):
        found = await engine_defs_svc.get_engine_def(ref)
        if found is None:
            found = await engine_defs_svc.get_engine_def_by_name(ref)
        return found

    with mock.patch.object(Session, "flow", _forbidden):
        graph, id_map = await compile_workflow_def(spec, resolve_engine_def=_resolve_engine_def)

    assert graph is not None
    assert id_map


# ---------------------------------------------------------------------------
# 7. Pure builder / non-execution assertions
# ---------------------------------------------------------------------------


def test_operation_graph_builder_stays_a_pure_builder():
    from lionagi.operations.builder import OperationGraphBuilder

    assert not hasattr(OperationGraphBuilder, "execute")
    assert not hasattr(OperationGraphBuilder, "flow")


def test_pattern_graph_builders_are_synchronous_pure_constructors():
    from lionagi.orchestration.patterns import build_dag_graph, build_fanout_graph

    assert inspect.iscoroutinefunction(build_fanout_graph) is False
    assert inspect.iscoroutinefunction(build_dag_graph) is False


def test_visualize_graph_never_references_flow_execution():
    from lionagi.operations._visualize_graph import visualize_graph

    source = inspect.getsource(visualize_graph)
    assert ".flow(" not in source
    assert ".flow_stream(" not in source
    assert ".run_dag(" not in source
