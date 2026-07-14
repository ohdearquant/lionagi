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
enclosing scope's provenance, first masking every name any local import,
assignment, ``for``/``async for`` target, match-pattern capture, or
augmented assignment in the function body binds -- ANYWHERE in the body,
conditional or not, since Python function scope is not statement-ordered: a
name bound only inside an ``if``/``try``/``for``/``match``-case is still a
lexical local for the whole function, so an inherited same-named binding
must not leak through (masking-only binder forms never establish or restore
provenance themselves) -- and
also discarding any parameter-shadowed name, then replays its OWN body's
UNCONDITIONAL binding events (in source order) on top of that masked copy —
so a function-local reimport genuinely restores what a same-named parameter
(or a conditional in-body import of the same name) masked. Within a scope, a
binding event nested inside an ``if``/``try``/``except``/``for``/``while``/
``with``/``match``-case block is CONDITIONAL: it may only DISCARD provenance
(a conditional rebinding to something unrecognized is treated as if it might
have executed, since trusting the name afterward risks a false-positive
executor site) and may never ESTABLISH or RESTORE it (a conditional import
might never execute, so applying it anyway risks the same false positive
from the other direction) — only an unconditional binding, a direct
statement of the scope's own body, can establish or restore provenance. It
does not perform general data-flow analysis: resolution through a factory
function's return value, a non-literal string argument, an alias threaded
through more than one intermediate assignment, or any binding inside a
comprehension or walrus assignment is not tracked and remains a residual
imprecision. (Class bodies ARE tracked: each gets its own add-only
environment -- see ``_SinkVisitor.visit_ClassDef``.)

A name declared ``global``/``nonlocal`` anywhere in a function's own body is
NOT a lexical local of that function and must not be masked the way a
genuine local is, but the two declarations resolve against different target
scopes and are handled accordingly: ``global`` overlays provenance from a
pristine MODULE-scope snapshot rather than the lexical-enclosing scope (a
`global NAME` reference skips every intermediate function scope, including
one whose own parameter or local happens to shadow the same name), while
``nonlocal`` resolves against the nearest enclosing FUNCTION scope, which the
ordinary lexical inheritance chain already reproduces without any extra
overlay. A ``global``/``nonlocal`` statement nested inside a CLASS body does
not count as a declaration of the surrounding function at all -- a class
body is its own namespace for this purpose, unlike the ordinary binding
events collected within it. This scanner's governing invariant is
ZERO-FALSE-NEGATIVES: a missed executor-construction site is a coverage hole
(dangerous), while a spurious one only costs a review (safe); every
global/nonlocal refinement above exists to close a false-negative, and where
closing a remaining false-positive would require reasoning about whether a
declared name's own binder form (a ``for``-target, match-capture, ``with``/
``except ... as``, ``del``, or augmented assignment reached under the
declaration) actually executes, that reasoning is deliberately NOT attempted
-- the scanner keeps the (possibly stale) inherited/overlaid provenance and
reports a site, a documented conservative over-approximation pinned by
``test_global_declared_executing_binder_is_conservative_overapproximation``.

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
import sys
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


@dataclass(frozen=True)
class _NameBindingEvent:
    """A binder form that makes *name* a lexical local of the enclosing
    scope but is never recognized as establishing/restoring import
    provenance: a ``for``/``async for`` statement target, a match-pattern
    capture, an augmented-assignment target, a ``with``/``async with`` ``as``
    target, an ``except ... as`` handler name, or a ``del`` target. Carries
    only the name (plus position, so it sorts alongside the other event
    kinds) -- ``_replay_binding_events`` always skips it (these forms only
    MASK; see ``_scope_bound_names``), it exists purely to feed the mask
    collection."""

    name: str
    lineno: int
    col_offset: int


_BindingEvent = tuple[ast.Import | ast.ImportFrom | ast.Assign | _NameBindingEvent, bool]


def _store_names(target: ast.expr) -> set[str]:
    """Every ``Name`` bound (Store context) anywhere inside *target*,
    including tuple/list unpacking (``for (a, importlib) in ...``). A
    Name embedded in an Attribute/Subscript base (``obj`` in ``obj.attr``)
    is Load context, not Store, so ``ast.walk`` naturally excludes it --
    only genuine new local bindings are collected."""
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }


def _pattern_capture_names(pattern: ast.pattern) -> set[str]:
    """Every name a match *pattern* binds as a capture: bare-name captures
    and as-patterns (``MatchAs.name``, including under a ``MatchOr`` --
    ``case {"x": importlib} | {"y": importlib}:`` is two ``MatchMapping``
    patterns each holding a ``MatchAs`` capture named ``importlib``, both
    found by walking), star captures in a sequence pattern (``MatchStar.
    name``), and the ``**rest`` capture of a mapping pattern (``MatchMapping.
    rest``). Match patterns never contain a nested function/lambda scope, so
    a plain ``ast.walk`` is safe here."""
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.add(node.rest)
    return names


# Both try-statement forms carry the same body/handlers/orelse/finalbody
# slots and must be traversed identically -- a `global` declaration or a
# binding inside a `try`/`except*` suite belongs to the enclosing function
# scope exactly like its plain-`try` counterpart. `ast.TryStar` only exists
# on Python 3.11+, so reference it conditionally.
_TRY_STMT_TYPES: tuple[type[ast.stmt], ...] = (
    (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)
)


def _scope_declared_names(stmts: list[ast.stmt]) -> tuple[set[str], set[str]]:
    """Every name declared ``global`` and, separately, every name declared
    ``nonlocal``, anywhere in ONE lexical scope's statement list *stmts* --
    returned as ``(global_names, nonlocal_names)`` since the two declarations
    resolve against DIFFERENT target scopes (see ``_SinkVisitor._push_func_scope``:
    ``global`` overlays provenance from the MODULE scope, ``nonlocal`` keeps
    inheriting from the lexical-enclosing scope) and must not be merged into
    one set the way an earlier version of this function did.

    Descends transparently into control-flow bodies -- a ``global``/
    ``nonlocal`` nested inside an ``if``/``try``/``try``-``except*``/``for``/
    ``while``/``with``/``match``-case still applies to the whole enclosing
    function scope (a ``global`` statement is a compile-time scope directive;
    its surrounding control flow never conditions it) -- but STOPS at a
    nested ``FunctionDef``/``AsyncFunctionDef``/``Lambda`` (those own their
    own declarations) AND at a nested ``ClassDef``: a class body is its own
    namespace, so ``global x`` there only redirects lookups of ``x`` within
    the CLASS body's own execution; it does not declare the surrounding
    function's same-named binding at all. Descending into the class body
    here would wrongly un-mask a same-named local the enclosing function
    itself never declared global/nonlocal (see
    test_class_body_global_declaration_does_not_leak_into_enclosing_function).
    ``_collect_scope_binding_events`` stops at ``ClassDef`` for the matching
    reason on the binding side: an ordinary assignment in a class body binds
    the CLASS namespace, never the enclosing function's; the class body gets
    its own environment in ``_SinkVisitor.visit_ClassDef``."""
    global_names: set[str] = set()
    nonlocal_names: set[str] = set()

    def _merge(body: list[ast.stmt]) -> None:
        g, n = _scope_declared_names(body)
        global_names.update(g)
        nonlocal_names.update(n)

    for stmt in stmts:
        if isinstance(stmt, ast.Global):
            global_names.update(stmt.names)
        elif isinstance(stmt, ast.Nonlocal):
            nonlocal_names.update(stmt.names)
        elif isinstance(stmt, ast.If):
            _merge(stmt.body)
            _merge(stmt.orelse)
        elif isinstance(stmt, _TRY_STMT_TYPES):
            _merge(stmt.body)
            for handler in stmt.handlers:
                _merge(handler.body)
            _merge(stmt.orelse)
            _merge(stmt.finalbody)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            _merge(stmt.body)
            _merge(stmt.orelse)
        elif isinstance(stmt, ast.While):
            _merge(stmt.body)
            _merge(stmt.orelse)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _merge(stmt.body)
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                _merge(case.body)
        # ClassDef: its own namespace for global/nonlocal purposes: stop here
        # (see docstring divergence note above).
        # FunctionDef/AsyncFunctionDef/Lambda: a new scope: stop here.
    return global_names, nonlocal_names


def _collect_scope_binding_events(
    stmts: list[ast.stmt], conditional: bool, events: list[_BindingEvent]
) -> None:
    """Collect Import/ImportFrom/simple-Name-Assign binding events (an
    annotated single-Name assignment with a value counts as one too), plus
    mask-only ``_NameBindingEvent``s for AugAssign targets and bare
    annotations (``name: T`` binds nothing at runtime but is a lexical
    local of the whole scope), out of *stmts* --
    the statement list of ONE lexical scope (a module body or a function
    body) -- tagging each with whether it is CONDITIONAL: nested inside an
    ``if``/``try``/``except``/``for``/``while``/``with`` body, or inside any
    ``match`` statement's case body (a case might not match, so every case
    body is conditional regardless of which case, if any, is the one that
    runs), rather than a direct statement of *stmts* itself. Also emits
    mask-only ``_NameBindingEvent``s (always CONDITIONAL, since the
    surrounding loop/case/with/handler may not execute) for ``for``/``async
    for`` statement targets, match-pattern captures, every ``with``/``async
    with`` item's ``as`` target (reusing the same Store-name walker as
    for-targets, so tuple/destructuring ``as (a, b)`` is collected too), and
    every ``except ... as NAME`` handler name -- these ARE lexical locals of
    the enclosing function scope (unlike a comprehension target, which is its
    own separate scope and is never walked here since comprehensions are
    expressions, not statements). A ``del NAME`` target gets the same
    mask-only treatment but tagged with the CURRENT *conditional* value
    (like ``AugAssign``, it is a direct statement of *stmts*, not a nested
    body). Stops at a nested class body (its assignments and imports bind the
    CLASS namespace, not this scope; ``_SinkVisitor.visit_ClassDef`` gives it
    its own environment) and at a nested function, async function, or
    lambda -- those get their own independent scope when ``_SinkVisitor``
    reaches them, each replaying only its own body's events on top of a copy
    of its enclosing scope's provenance."""
    for stmt in stmts:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            events.append((stmt, conditional))
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            events.append((stmt, conditional))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            # An annotated assignment WITH a value binds exactly like a
            # simple assignment (it can establish, restore, or clear
            # provenance, so it must be a full event -- an annotated
            # `name: object = importlib.import_module(...)` establishing
            # provenance would otherwise be invisible and its construction
            # site missed). A BARE annotation (`name: object`) never binds
            # at runtime but still makes the name a lexical local of the
            # whole function scope, so it is a mask-only event.
            if stmt.value is not None:
                events.append((stmt, conditional))
            else:
                events.append(
                    (
                        _NameBindingEvent(stmt.target.id, stmt.lineno, stmt.col_offset),
                        conditional,
                    )
                )
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            events.append(
                (
                    _NameBindingEvent(stmt.target.id, stmt.lineno, stmt.col_offset),
                    conditional,
                )
            )
        elif isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    events.append(
                        (
                            _NameBindingEvent(target.id, stmt.lineno, stmt.col_offset),
                            conditional,
                        )
                    )

        if isinstance(stmt, ast.If):
            _collect_scope_binding_events(stmt.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
        elif isinstance(stmt, _TRY_STMT_TYPES):
            _collect_scope_binding_events(stmt.body, True, events)
            for handler in stmt.handlers:
                if handler.name is not None:
                    events.append(
                        (
                            _NameBindingEvent(handler.name, handler.lineno, handler.col_offset),
                            True,
                        )
                    )
                _collect_scope_binding_events(handler.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
            _collect_scope_binding_events(stmt.finalbody, True, events)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            for name in _store_names(stmt.target):
                events.append((_NameBindingEvent(name, stmt.lineno, stmt.col_offset), True))
            _collect_scope_binding_events(stmt.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
        elif isinstance(stmt, ast.While):
            _collect_scope_binding_events(stmt.body, True, events)
            _collect_scope_binding_events(stmt.orelse, True, events)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    for name in _store_names(item.optional_vars):
                        events.append((_NameBindingEvent(name, stmt.lineno, stmt.col_offset), True))
            _collect_scope_binding_events(stmt.body, True, events)
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                for name in _pattern_capture_names(case.pattern):
                    events.append((_NameBindingEvent(name, stmt.lineno, stmt.col_offset), True))
                _collect_scope_binding_events(case.body, True, events)
        # ClassDef: an ordinary assignment (or import) in a class body binds
        # the CLASS namespace, never the enclosing function's -- Python gives
        # the class body its own namespace, so emitting its binders here
        # would wrongly MASK a same-named enclosing binding and hide a real
        # construction site in the enclosing function (see
        # test_class_body_assignment_does_not_mask_enclosing_scope_name).
        # The class body's own view is modeled by _SinkVisitor.visit_ClassDef.
        # The one thing this deliberately gives up: a `global x` declaration
        # INSIDE the class body can make a class-body assignment rebind the
        # module's x at class-definition time, which this scope's replay now
        # never sees -- but not clearing module provenance there keeps stale-
        # but-possibly-correct provenance, which can only ADD a reported
        # site, never miss a real one (the safe direction under this
        # scanner's zero-false-negatives contract).
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
        if isinstance(node, _NameBindingEvent):
            # for-target / match-capture / augassign-target: a lexical local
            # for masking purposes only (see _scope_bound_names) -- never
            # recognized as establishing or restoring import provenance.
            continue
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
            # ast.Assign (single Name target) or ast.AnnAssign (Name target,
            # non-None value) -- collection guarantees no other shape lands
            # here, and both carry the binding in `.value`.
            target = node.target.id if isinstance(node, ast.AnnAssign) else node.targets[0].id
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


def _scope_bound_names(events: list[_BindingEvent]) -> set[str]:
    """Every name that some binding statement collected in *events* assigns
    at runtime -- CONDITIONAL bindings included. Used to mask a function
    scope's inherited provenance before replaying its own unconditional
    events: Python function scope is not statement-ordered, so a name
    imported or assigned only inside an ``if``/``try``/``for``/``match``-case
    anywhere in the body is still a lexical LOCAL for the ENTIRE function --
    never the enclosing scope's same-named binding -- even on a code path
    where that conditional statement never executes and the name ends up
    merely unbound. Trusting an inherited binding for such a name would be
    the same false-positive shape a conditional import already guards
    against, just leaking in from the other direction (down from the
    enclosing scope instead of sideways within one)."""
    names: set[str] = set()
    for node, _conditional in events:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, _NameBindingEvent):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign):
            names.add(node.target.id)
        else:
            names.add(node.targets[0].id)
    return names


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
    recognition to receivers that provably denote the flow module. This pair
    also becomes index 0 of ``_SinkVisitor``'s scope stacks (the pristine
    MODULE-scope snapshot that never gets mutated in place -- see
    ``_SinkVisitor.__init__``/``_push_func_scope``), which a ``global``
    declaration inside any nested function scope overlays its provenance
    from, rather than the lexical-enclosing scope."""
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
    which can hold no statements), that copy is then further masked for
    every name the function's OWN body binds ANYWHERE -- via
    ``_scope_bound_names``, conditional bindings included, since a name
    bound only inside an ``if``/``try``/``match``-case is still a lexical
    local for the whole function and must not fall through to the enclosing
    scope's same-named binding -- and only then advanced by replaying the
    body's UNCONDITIONAL binding events on top of that masked copy, via the
    same ``_collect_scope_binding_events``/``_replay_binding_events``
    machinery ``_collect_constructor_import_bindings`` uses for the module
    scope. Net effect: a genuine unconditional in-body reimport restores
    provenance a same-named parameter shadowed (exactly as a module-level
    reimport restores provenance an unrelated rebinding discarded), while a
    conditional in-body reimport of an inherited name masks it and leaves it
    masked -- it does not leak the outer scope's provenance through. The
    scope is popped again on the way out, restoring the enclosing scope's
    view for sibling functions.

    A name declared ``global``/``nonlocal`` anywhere in a function's OWN body
    (via ``_scope_declared_names``, which stops at a nested class body -- see
    its docstring) is exempt from that masking pass, since it is not a
    lexical local at all. The two declarations resolve against DIFFERENT
    target scopes and are handled accordingly: ``global`` is overlaid from a
    pristine MODULE-scope snapshot (index 0 of the scope stacks, never
    mutated in place) rather than inherited from the lexical-enclosing
    scope's copy, since a `global NAME` reference skips every intermediate
    function scope no matter what any of them locally bind NAME to;
    ``nonlocal`` needs no such overlay because it resolves against the
    nearest ENCLOSING FUNCTION scope, which the ordinary lexical copy-chain
    already reproduces by construction. See the module docstring's
    zero-false-negatives invariant and ``_push_func_scope`` for the full
    argument, and the ``test_global_declared_*``/``test_nonlocal_declared_*``
    regressions for both directions of each.

    A declared name's own binder forms (``for``-target, match-capture,
    ``with``/``except ... as``, ``del``, augmented assignment) are still only
    ever mask-only events in ``_replay_binding_events`` -- they never
    establish or restore provenance there, so a declared name whose ONLY
    in-scope binder is one of these forms keeps whatever provenance the
    overlay/inheritance step already gave it, even though that binder form
    may actually execute and rebind/clear the real module or enclosing
    binding at runtime. This is a DELIBERATE, documented conservative
    over-approximation, not a bug: this scanner's contract is
    zero-false-negatives (a missed executor-construction site is dangerous; a
    spurious one only costs a review), so keeping stale-but-possibly-correct
    provenance through a maybe-executing declared binder can only ever ADD a
    reported site, never drop one that is real. Eliminating this residual
    would require reasoning about whether the binder actually executes --
    genuine dead/live-code analysis -- which this scanner intentionally does
    not do; see
    test_global_declared_executing_binder_is_conservative_overapproximation,
    which pins this exact shape as an expected, named false-positive so a
    future change that trades it for a missed site fails that test instead
    of quietly regressing.

    The class-body environment (``visit_ClassDef``) applies the same
    principle system-wide: it is built ADD-ONLY because adding provenance
    can only add candidate sites, and adding sites is the false-positive-
    safe side of the zero-false-negatives invariant -- every deliberate
    imprecision in this scanner errs in that one direction."""

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
        """Class bodies get their own environment, built ADD-ONLY on top of
        the enclosing scope's view.

        A class body is its own namespace but NOT a function scope: name
        resolution inside it is SEQUENTIAL (a read before any class-local
        binding of that name falls through to the enclosing scope; there is
        no scope-wide "assigned anywhere means local everywhere" rule the
        way function bodies have). That rule is what justifies function
        scopes' mask-then-replay model -- a use before an unconditional
        rebinding is an UnboundLocalError at runtime, so no real construction
        is lost by masking. Class bodies offer no such justification, so any
        mask or provenance-clear applied to the class view could hide a
        construction that really reads the enclosing binding. The class
        environment is therefore strictly additive relative to the enclosing
        scope:

        - no ``_scope_bound_names`` masking at all;
        - every establishing event in the class body applies, conditional
          ones included (trusting a maybe-executed class-body import can
          only ADD a reported site, the safe direction);
        - after replaying the class body's own events on private copies, the
          enclosing view is unioned back in, so a replay-applied clear never
          drops enclosing-inherited provenance;
        - a class-body ``global`` declaration overlays MODULE-scope
          provenance additively (class-body lookups of that name go to the
          module even when an enclosing function masked it) -- and the
          overlay is re-applied AFTER replay too, so a class-body rebind of
          a global-declared name (conditional or not, since replay here is
          forced unconditional) can never erase the module baseline the
          declaration grants;
        - the class body's OWN establishing events for global-declared
          names (a recognized import or constructor/module alias gained
          during replay relative to the pre-replay snapshot) are propagated
          additively to every view currently on the scope stacks, module
          snapshot included: a ``global x`` establishment executes at
          class-definition time and genuinely rebinds the MODULE's ``x``,
          which statements after the class definition (in the enclosing
          function, and via later ``global`` overlays anywhere) really read.
          Only establishing deltas propagate -- never clears (add-only), and
          never the module overlay the class merely inherited (propagating
          that would un-mask enclosing locals the declaration does not
          touch). On the single-valued constructor-alias channel the
          establishment OVERWRITES each view's existing attribution (a
          stale alias kept in a globally-resolving view would hide the real
          post-rebind executor construction), except that an executor
          attribution is never replaced by a non-executor one -- the
          attribution may only move toward reporting an executor, so every
          direction of rebind errs reportward.

        Ordinary (undeclared) class-body binders never leak OUT to the
        enclosing scope -- ``_collect_scope_binding_events`` stops at
        ``ClassDef`` -- and the class environment is popped when the body is
        done."""
        self._stack.append(node.name)
        enclosing_env = self._flow_env
        enclosing_bound = self._constructor_aliases
        env = enclosing_env.copy()
        bound = dict(enclosing_bound)
        events: list[_BindingEvent] = []
        _collect_scope_binding_events(node.body, False, events)
        global_names, _ = _scope_declared_names(node.body)
        module_env = self._flow_env_stack[0]
        module_bound = self._bound_stack[0]

        def _overlay_module_globals(*, overwrite: bool) -> None:
            # Pre-replay (overwrite=True): a global-declared name reads the
            # MODULE binding, never the lexical one the enclosing copy
            # carried, so the module attribution replaces it outright.
            # Post-replay (overwrite=False): only RESTORE a name the forced-
            # unconditional replay cleared -- an attribution the replay
            # itself established is later in source order and must win.
            for name in global_names:
                if name in module_env.names:
                    env.names.add(name)
                if name in module_env.lionagi_roots:
                    env.lionagi_roots.add(name)
                if name in module_env.import_module_funcs:
                    env.import_module_funcs.add(name)
                if name in module_env.importlib_mods:
                    env.importlib_mods.add(name)
                if name in module_bound:
                    if overwrite:
                        bound[name] = module_bound[name]
                    else:
                        bound.setdefault(name, module_bound[name])

        _overlay_module_globals(overwrite=True)
        # Snapshot the pre-replay view so the propagation step below can
        # distinguish what the class body itself ESTABLISHED (delta vs this
        # snapshot) from what it merely inherited via the overlay/enclosing
        # copy -- only the class body's own establishments rebind the module.
        pre_replay = env.copy()
        pre_replay_bound = dict(bound)
        # Force every event unconditional so replay applies establishes that
        # sit inside an if/try/for body too -- in a class body a conditional
        # import that DOES execute is readable by later class-body
        # statements, and skipping it (the function-scope rule) would miss a
        # real construction. Clears still apply, then the overlay re-run and
        # enclosing union below restore anything they dropped.
        _replay_binding_events([(evt, False) for evt, _ in events], bound, env)
        # Re-apply the module overlay AFTER replay: a class-body rebind of a
        # global-declared name (forced unconditional above, so even an
        # `if False:` assignment clears) must not erase the module baseline
        # the `global` declaration grants -- dropping it here is exactly the
        # missed-real-site direction the contract forbids.
        _overlay_module_globals(overwrite=False)
        env.names |= enclosing_env.names
        env.lionagi_roots |= enclosing_env.lionagi_roots
        env.import_module_funcs |= enclosing_env.import_module_funcs
        env.importlib_mods |= enclosing_env.importlib_mods
        for name, target in enclosing_bound.items():
            bound.setdefault(name, target)
        # A class body executes at definition time, so an ESTABLISHING event
        # for a global-declared name (`global x` + `import x` / recognized
        # alias assignment) genuinely rebinds the MODULE's `x` -- statements
        # after the class definition really read it. Propagate those
        # establishments (the delta the replay added relative to the
        # pre-replay snapshot, restricted to declared names) to every view on
        # the scope stacks, module snapshot included. The provenance channels
        # are pure sets, so their propagation is additive by construction:
        # clears never propagate, and the inherited overlay is excluded by
        # the delta (propagating it would un-mask enclosing locals the
        # declaration does not touch -- see
        # test_class_body_global_declaration_does_not_leak_into_enclosing_function).
        # The ALIAS channel is single-valued, so "add-only" there means the
        # attribution may only move TOWARD reporting an executor, never away
        # from one: the fresh module binding OVERWRITES a view's existing
        # attribution (a globally-resolving read after the class definition
        # really constructs the new target, and keeping the stale alias
        # would hide a real executor site) -- UNLESS the overwrite would
        # replace an executor attribution with a non-executor one, in which
        # case the executor attribution is kept. That bias is what makes the
        # rule safe without resolving, per view, whether the name is a
        # lexical local, a closure read, or a module read there (and safe
        # against the temporal ambiguity a resolution walk cannot decide: a
        # globally-resolving read in an ANCESTOR frame may execute before or
        # after this class definition ever runs). Every cell errs reportward:
        # a view whose read really resolves to its own non-executor local
        # can gain a spurious executor candidate (review cost only), and no
        # rebind in either direction can ever remove an executor attribution.
        for name in global_names:
            established_channels = [
                channel
                for channel, pre_channel in (
                    (env.names, pre_replay.names),
                    (env.lionagi_roots, pre_replay.lionagi_roots),
                    (env.import_module_funcs, pre_replay.import_module_funcs),
                    (env.importlib_mods, pre_replay.importlib_mods),
                )
                if name in channel and name not in pre_channel
            ]
            established_alias = (
                bound.get(name)
                if name in bound and pre_replay_bound.get(name) != bound[name]
                else None
            )
            if not established_channels and established_alias is None:
                continue
            for view_env, view_bound in zip(self._flow_env_stack, self._bound_stack):
                if name in env.names and name not in pre_replay.names:
                    view_env.names.add(name)
                if name in env.lionagi_roots and name not in pre_replay.lionagi_roots:
                    view_env.lionagi_roots.add(name)
                if name in env.import_module_funcs and name not in pre_replay.import_module_funcs:
                    view_env.import_module_funcs.add(name)
                if name in env.importlib_mods and name not in pre_replay.importlib_mods:
                    view_env.importlib_mods.add(name)
                if established_alias is not None:
                    current = view_bound.get(name)
                    if (
                        current not in _EXECUTOR_CONSTRUCTOR_NAMES
                        or established_alias in _EXECUTOR_CONSTRUCTOR_NAMES
                    ):
                        view_bound[name] = established_alias
        self._flow_env_stack.append(env)
        self._bound_stack.append(bound)
        self.generic_visit(node)
        self._pop_func_scope()
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
            # Mask every name the body binds ANYWHERE (conditional included)
            # before replaying -- see _scope_bound_names: a name imported or
            # assigned only inside an if/try/match-case is still a lexical
            # local for the whole function, so an inherited same-named
            # binding from the enclosing scope must not leak through here.
            # EXCEPT a name declared `global`/`nonlocal` anywhere in this
            # scope's OWN body (see _scope_declared_names, which stops at a
            # nested class body -- a global/nonlocal statement inside a
            # nested class does NOT declare a name of THIS scope): such a
            # name is NOT a function-local at all, so it must not be
            # discarded from inherited provenance the way a genuine local is.
            global_names, nonlocal_names = _scope_declared_names(node.body)
            declared = global_names | nonlocal_names
            for name in _scope_bound_names(events) - declared:
                env.discard(name)
                bound.pop(name, None)
            # A `global NAME` declaration resolves EVERY reference and
            # rebinding of NAME in this function against the MODULE scope,
            # never the lexical-enclosing scope -- even when an enclosing
            # function's own parameter or local binding shadows the same
            # name for ITS OWN body (see
            # test_global_declared_name_resolves_against_module_scope_not_shadowed_param).
            # `env`/`bound` at this point still hold whatever the lexical
            # parent's copy carried (correct for `nonlocal`, wrong for
            # `global`), so every global-declared name is first cleared and
            # then overlaid from a MODULE-scope snapshot -- the bottom of
            # the scope stack (index 0). Function-scope replays never mutate
            # it in place (only ever copies further down; see
            # _flow_env.copy()/dict(self._constructor_aliases) above); the
            # ONLY in-place updates it ever receives are class-body global
            # establishments (see visit_ClassDef) -- additive on the
            # provenance sets, and on the alias channel a genuine module
            # rebinding executed at class-definition time may replace a
            # non-executor attribution (never an executor one) -- so it
            # always reflects the module's own bindings regardless of how
            # many function scopes are currently pushed.
            # `nonlocal`-declared names need no such overlay: nonlocal
            # resolves against the nearest ENCLOSING FUNCTION scope, and
            # `env`/`bound` here already start as a copy of that exact
            # lexical parent's (already fully masked-and-replayed) view, so
            # inheritance alone reproduces nonlocal's real target by
            # construction (verified by
            # test_nonlocal_declared_name_not_masked_discovers_executor and
            # its masked-outer control, test_nonlocal_declared_name_with_masked_outer_binding_finds_no_site).
            module_env = self._flow_env_stack[0]
            module_bound = self._bound_stack[0]
            for name in global_names:
                env.discard(name)
                bound.pop(name, None)
                if name in module_env.names:
                    env.names.add(name)
                if name in module_env.lionagi_roots:
                    env.lionagi_roots.add(name)
                if name in module_env.import_module_funcs:
                    env.import_module_funcs.add(name)
                if name in module_env.importlib_mods:
                    env.importlib_mods.add(name)
                if name in module_bound:
                    bound[name] = module_bound[name]
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


def test_match_case_clear_and_sibling_case_restore_is_not_misreported(tmp_path):
    """Regression for the match/case conditional-provenance false positive:
    a `match` statement's cases are exactly as conditional as an if/elif
    chain -- only one case (if any) actually runs -- so a `case` that rebinds
    `importlib` to something unrecognized and a SIBLING `case` that
    re-imports it must NOT combine into a discovered site, for the same
    reason an if/else rebind-then-reimport does not: the checker cannot know
    which case matched, so the conservative reading nets no provenance.
    Before recursing into `ast.Match`, `_collect_scope_binding_events` never
    walked into case bodies at all, so this module-level match was invisible
    to the module-scope pass and the (accidentally still-unconditional)
    original `import importlib` provenance was trusted straight through --
    wrongly reporting a site."""
    rogue = tmp_path / "match_clear_sibling_restore.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "match selection:\n"
        "    case 1:\n"
        "        importlib = object()\n"
        "    case _:\n"
        "        import importlib\n\n\n"
        "async def run(session, graph):\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a match/case rebind-then-conditionally-reimport must not register as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )
    assert ("match_clear_sibling_restore.py", "run") not in discovery.call_sites


def test_conditional_in_body_reimport_does_not_leak_outer_importlib_provenance(tmp_path):
    """Regression for the conditional-in-body-import masking false positive:
    a conditional `import importlib` inside a function body (`if False:
    import importlib`) is still a Python lexical LOCAL for the whole
    function -- at runtime the name is merely unbound on the branch that
    doesn't run, never the module-level `importlib`. The pushed function
    scope must MASK the inherited outer provenance for `importlib` (not just
    skip applying the conditional import, which left the inherited binding
    trusted) so this does NOT register a site."""
    rogue = tmp_path / "conditional_inbody_importlib_no_leak.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    if False:\n"
        "        import importlib\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a conditional in-body reimport must mask, not inherit, outer importlib provenance "
        f"— got: {sorted(discovery.executor_sites)}"
    )
    assert ("conditional_inbody_importlib_no_leak.py", "run") not in discovery.call_sites


def test_conditional_in_body_reimport_does_not_leak_outer_lionagi_root_provenance(tmp_path):
    """Same conditional-in-body masking false positive for the `lionagi`
    package root, dotted-attribute shape: `if False: import lionagi` inside
    the function body is still a lexical local for the whole function and
    must mask the module-level `import lionagi`'s provenance rather than
    leave it trusted."""
    rogue = tmp_path / "conditional_inbody_lionagi_no_leak.py"
    rogue.write_text(
        "import lionagi\n\n\n"
        "async def run(session, graph):\n"
        "    if False:\n"
        "        import lionagi\n"
        '    return await getattr(lionagi.operations.flow, "DependencyAwareExecutor")(\n'
        "        session, graph\n"
        "    ).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a conditional in-body reimport must mask, not inherit, outer lionagi-root "
        f"provenance — got: {sorted(discovery.executor_sites)}"
    )
    assert ("conditional_inbody_lionagi_no_leak.py", "run") not in discovery.call_sites


def test_conditional_in_body_alias_reimport_does_not_leak_outer_constructor_alias(tmp_path):
    """Same conditional-in-body masking false positive for a constructor
    alias: a module-level `from lionagi.operations.flow import
    DependencyAwareExecutor as X` establishes an unconditional alias, but a
    conditional `if cond: from somewhere import DependencyAwareExecutor as X`
    inside the function body is still a lexical local `X` for the whole
    function -- the local binding must mask the outer alias rather than let
    it leak through to the `X(...)` call."""
    rogue = tmp_path / "conditional_inbody_alias_no_leak.py"
    rogue.write_text(
        "from lionagi.operations.flow import DependencyAwareExecutor as X\n\n\n"
        "async def run(cond, session, graph):\n"
        "    if cond:\n"
        "        from somewhere import DependencyAwareExecutor as X\n"
        "    return await X(session, graph).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a conditional in-body alias reimport must mask, not inherit, the outer constructor "
        f"alias — got: {sorted(discovery.executor_sites)}"
    )
    assert ("conditional_inbody_alias_no_leak.py", "run") not in discovery.call_sites


def test_match_or_pattern_capture_masks_inherited_importlib_provenance(tmp_path):
    """Regression for the under-collection defect: a match-pattern capture
    binds a name just as a Name-Assign target does -- ``case {"x": importlib}
    | {"y": importlib}:`` binds ``importlib`` in both alternatives of the
    or-pattern -- so it must mask the module-level `import importlib` for
    the rest of the function, exactly like a conditional in-body reimport
    already does. Verified against Python's own `symtable`:
    `importlib.is_local() == True` for this function."""
    import symtable as _symtable

    rogue = tmp_path / "match_or_pattern_capture.py"
    source = (
        "import importlib\n\n\n"
        "async def run(value, session, graph):\n"
        "    match value:\n"
        '        case {"x": importlib} | {"y": importlib}:\n'
        "            pass\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (func_table,) = (c for c in top.get_children() if c.get_name() == "run")
    assert func_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a match-or-pattern capture must mask, not inherit, outer importlib provenance — "
        f"got: {sorted(discovery.executor_sites)}"
    )
    assert ("match_or_pattern_capture.py", "run") not in discovery.call_sites


def test_for_target_masks_inherited_importlib_provenance(tmp_path):
    """Regression for the under-collection defect: a ``for``/``async for``
    STATEMENT target is a lexical local of the enclosing function (unlike a
    comprehension target, which is its own separate scope and must not be
    collected) -- ``for importlib in ():`` binds ``importlib`` and must mask
    the module-level import for the whole function body. Verified against
    `symtable`: `importlib.is_local() == True`."""
    import symtable as _symtable

    rogue = tmp_path / "for_target_masks.py"
    source = (
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    for importlib in ():\n"
        "        pass\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (func_table,) = (c for c in top.get_children() if c.get_name() == "run")
    assert func_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a for-loop target must mask, not inherit, outer importlib provenance — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("for_target_masks.py", "run") not in discovery.call_sites


def test_augmented_assignment_masks_inherited_importlib_provenance(tmp_path):
    """Regression for the under-collection defect: `importlib += 1` is a
    Name-target binding (`AugAssign`, not `Assign`) and must mask the
    module-level import for the whole function body the same way a plain
    conditional rebind does. Verified against `symtable`: `importlib.
    is_local() == True`."""
    import symtable as _symtable

    rogue = tmp_path / "augassign_masks.py"
    source = (
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    if False:\n"
        "        importlib += 1\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (func_table,) = (c for c in top.get_children() if c.get_name() == "run")
    assert func_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "an augmented-assignment target must mask, not inherit, outer importlib provenance "
        f"— got: {sorted(discovery.executor_sites)}"
    )
    assert ("augassign_masks.py", "run") not in discovery.call_sites


def test_for_target_masks_then_unconditional_reimport_restores_discovery(tmp_path):
    """Companion to test_for_target_masks_inherited_importlib_provenance:
    masking is not permanent -- a genuine UNCONDITIONAL `import importlib`
    later in the same function body must still restore provenance and yield
    a site, exactly like any other in-body reimport restoring a
    parameter-shadowed or conditionally-masked name."""
    rogue = tmp_path / "for_target_then_reimport.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    for importlib in ():\n"
        "        pass\n"
        "    import importlib\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("for_target_then_reimport.py", "run")
    assert key in discovery.call_sites, (
        "an unconditional reimport after a for-target mask was not discovered — found: "
        f"{sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the reimport-restored dynamic lookup after a for-target mask must be flagged as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )


def test_for_target_masks_inherited_lionagi_root_provenance_dotted(tmp_path):
    """Dotted-provenance variant of the for-target masking regression: the
    mask keys on the bound NAME, so a for-target named `lionagi` must mask
    the module-level `import lionagi` root for a dotted
    `lionagi.operations.flow` receiver just as it does for the `importlib`
    single-name provenance kind."""
    rogue = tmp_path / "for_target_masks_lionagi_root.py"
    rogue.write_text(
        "import lionagi\n\n\n"
        "async def run(session, graph):\n"
        "    for lionagi in ():\n"
        "        pass\n"
        '    return await getattr(lionagi.operations.flow, "DependencyAwareExecutor")(\n'
        "        session, graph\n"
        "    ).execute()\n"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a for-target named lionagi must mask, not inherit, the lionagi-root provenance — "
        f"got: {sorted(discovery.executor_sites)}"
    )
    assert ("for_target_masks_lionagi_root.py", "run") not in discovery.call_sites


def test_for_target_tuple_unpack_masks_inherited_importlib_provenance(tmp_path):
    """Tuple-unpacking variant: `for (a, importlib) in ...:` binds BOTH `a`
    and `importlib` as lexical locals of the enclosing function -- every
    `Name` in Store context nested inside the for-target must be collected,
    not just a single bare-name target."""
    rogue = tmp_path / "for_target_tuple_unpack.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    for a, importlib in [(1, 2)]:\n"
        "        pass\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a tuple-unpacking for-target must mask, not inherit, outer importlib provenance — "
        f"got: {sorted(discovery.executor_sites)}"
    )
    assert ("for_target_tuple_unpack.py", "run") not in discovery.call_sites


def test_with_as_target_masks_inherited_importlib_provenance(tmp_path):
    """Regression for the under-collection defect: a `with ... as NAME`
    target is a lexical local of the enclosing function scope (a
    Store-context ``Name`` in ``item.optional_vars``) and must mask the
    module-level import for the whole function body the same way a
    for-target does. Verified against `symtable`: `importlib.is_local() ==
    True`."""
    import symtable as _symtable

    rogue = tmp_path / "with_as_masks.py"
    source = (
        "import contextlib\n"
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    with contextlib.nullcontext() as importlib:\n"
        "        pass\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (func_table,) = (c for c in top.get_children() if c.get_name() == "run")
    assert func_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a with-as target must mask, not inherit, outer importlib provenance — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("with_as_masks.py", "run") not in discovery.call_sites


def test_with_as_masks_then_unconditional_reimport_restores_discovery(tmp_path):
    """Companion to test_with_as_target_masks_inherited_importlib_provenance:
    masking is not permanent -- a genuine UNCONDITIONAL `import importlib`
    later in the same function body must still restore provenance and yield
    a site, exactly like a for-target mask followed by a reimport does."""
    rogue = tmp_path / "with_as_then_reimport.py"
    rogue.write_text(
        "import contextlib\n"
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    with contextlib.nullcontext() as importlib:\n"
        "        pass\n"
        "    import importlib\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("with_as_then_reimport.py", "run")
    assert key in discovery.call_sites, (
        "an unconditional reimport after a with-as mask was not discovered — found: "
        f"{sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the reimport-restored dynamic lookup after a with-as mask must be flagged as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )


def test_except_as_target_masks_inherited_importlib_provenance(tmp_path):
    """Regression for the under-collection defect: an `except ... as NAME`
    handler binds `NAME` as a lexical local of the enclosing function scope
    (`ExceptHandler.name`, a plain str -- and even though the name is
    deleted again at the end of the handler, Python still treats it as a
    local for the WHOLE function) and must mask the module-level import for
    the whole function body. Verified against `symtable`: `importlib.
    is_local() == True`."""
    import symtable as _symtable

    rogue = tmp_path / "except_as_masks.py"
    source = (
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as importlib:\n"
        "        pass\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (func_table,) = (c for c in top.get_children() if c.get_name() == "run")
    assert func_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "an except-as target must mask, not inherit, outer importlib provenance — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("except_as_masks.py", "run") not in discovery.call_sites


def test_del_target_masks_inherited_importlib_provenance(tmp_path):
    """Regression for the under-collection defect: `del importlib` is a
    Del-context Name binding/unbinding target (`ast.Delete.targets`) and
    must mask the module-level import for the whole function body the same
    way a conditional rebind does. Verified against `symtable`: `importlib.
    is_local() == True`."""
    import symtable as _symtable

    rogue = tmp_path / "del_masks.py"
    source = (
        "import importlib\n\n\n"
        "async def run(session, graph):\n"
        "    if False:\n"
        "        del importlib\n"
        '    return await getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph).execute()\n'
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (func_table,) = (c for c in top.get_children() if c.get_name() == "run")
    assert func_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a del target must mask, not inherit, outer importlib provenance — got: "
        f"{sorted(discovery.executor_sites)}"
    )
    assert ("del_masks.py", "run") not in discovery.call_sites


def test_global_declared_name_not_masked_discovers_executor_with_as(tmp_path):
    """A name carrying a `global NAME` declaration is NOT a lexical local of
    the function -- Python resolves every reference and rebinding of it
    against the module-level binding, regardless of any `with ... as NAME`
    (or other binder-form) rebind nested in a branch that never actually
    runs. The masking pass must not discard inherited provenance for such a
    name: here `with contextlib.nullcontext() as importlib` never executes
    (guarded by `if False`), so `importlib` is still the module-level import
    at the getattr call, and this is a genuine executor construction site.
    Verified against `symtable`: `importlib.is_local() == False` for `run`,
    the opposite of the with-as-without-`global` case pinned by
    test_with_as_target_masks_inherited_importlib_provenance."""
    import symtable as _symtable

    rogue = tmp_path / "global_declared_with_as.py"
    source = (
        "import contextlib\n"
        "import importlib\n\n\n"
        "def run(session, graph):\n"
        "    global importlib\n"
        "    if False:\n"
        "        with contextlib.nullcontext() as importlib:\n"
        "            pass\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (func_table,) = (c for c in top.get_children() if c.get_name() == "run")
    assert not func_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("global_declared_with_as.py", "run")
    assert key in discovery.call_sites, (
        "a global-declared name must not be masked by a never-entered with-as rebind — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the global-declared importlib construction must be flagged as an executor site — "
        f"got: {sorted(discovery.executor_sites)}"
    )


def test_global_declared_name_not_masked_discovers_executor_except_as(tmp_path):
    """Same `global`-not-a-local principle for an `except ... as NAME`
    binder: the handler that would rebind `importlib` never fires (the `try`
    body raises nothing), so at the getattr call `importlib` is still the
    module-level import. The `global importlib` declaration must keep that
    provenance visible rather than masking it the way an ordinary (non-
    `global`) `except ... as importlib` does in
    test_except_as_target_masks_inherited_importlib_provenance."""
    rogue = tmp_path / "global_declared_except_as.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "def run(session, graph):\n"
        "    global importlib\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as importlib:\n"
        "        pass\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("global_declared_except_as.py", "run")
    assert key in discovery.call_sites, (
        "a global-declared name must not be masked by a never-fired except-as rebind — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the global-declared importlib construction must be flagged as an executor site — "
        f"got: {sorted(discovery.executor_sites)}"
    )


def test_global_declared_name_not_masked_discovers_executor_del(tmp_path):
    """Same `global`-not-a-local principle for `del NAME`: the `del
    importlib` inside `if False` never executes, so `importlib` is still
    bound at the getattr call. The `global importlib` declaration must keep
    that provenance visible rather than masking it the way an ordinary
    (non-`global`) `del importlib` does in
    test_del_target_masks_inherited_importlib_provenance."""
    rogue = tmp_path / "global_declared_del.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "def run(session, graph):\n"
        "    global importlib\n"
        "    if False:\n"
        "        del importlib\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("global_declared_del.py", "run")
    assert key in discovery.call_sites, (
        "a global-declared name must not be masked by a never-entered del rebind — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the global-declared importlib construction must be flagged as an executor site — "
        f"got: {sorted(discovery.executor_sites)}"
    )


def test_nonlocal_declared_name_not_masked_discovers_executor(tmp_path):
    """`nonlocal NAME` carries the same not-a-local principle one scope
    level up: an inner function that declares `nonlocal importlib` resolves
    every reference and rebinding of it against the OUTER function's
    binding, not its own. Here the inner function's `if False: del
    importlib` never executes, so `importlib` still denotes the outer
    function's `importlib` module object at the getattr call, and this is a
    genuine executor construction site. Verified against `symtable`:
    `importlib.is_local() == False` for the inner function."""
    import symtable as _symtable

    rogue = tmp_path / "nonlocal_declared.py"
    source = (
        "def outer(session, graph):\n"
        "    import importlib\n\n"
        "    def inner():\n"
        "        nonlocal importlib\n"
        "        if False:\n"
        "            del importlib\n"
        '        return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n\n'
        "    return inner()\n"
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    (inner_table,) = (c for c in outer_table.get_children() if c.get_name() == "inner")
    assert not inner_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("nonlocal_declared.py", "outer.inner")
    assert key in discovery.call_sites, (
        "a nonlocal-declared name must not be masked by a never-entered del rebind — "
        f"found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the nonlocal-declared importlib construction must be flagged as an executor site — "
        f"got: {sorted(discovery.executor_sites)}"
    )


def test_nonlocal_declared_name_with_masked_outer_binding_finds_no_site(tmp_path):
    """Control for test_nonlocal_declared_name_not_masked_discovers_executor,
    proving `nonlocal`'s resolution-by-lexical-inheritance by construction
    (Condition 2): `nonlocal` needs no MODULE-scope overlay because the
    pushed scope for `inner` starts as a COPY of `outer`'s own (already
    fully masked-and-replayed) view -- so whatever `outer` itself ended up
    with is exactly what `inner`'s nonlocal-declared name inherits. Here
    `outer`'s OWN body conditionally rebinds `importlib` to an unrecognized
    object (never restored by a real reimport), which masks -- not
    restores -- `outer`'s own copy of the module-level import per
    ordinary (non-declared) masking rules; `inner`'s `nonlocal importlib`
    then correctly inherits that masked-away state and finds no site,
    proving the chain resolves against the ENCLOSING FUNCTION's actual
    binding, not a fresh module lookup."""
    import symtable as _symtable

    rogue = tmp_path / "nonlocal_declared_masked_outer.py"
    source = (
        "import importlib\n\n\n"
        "def outer(session, graph):\n"
        "    if False:\n"
        "        importlib = object()\n\n"
        "    def inner():\n"
        "        nonlocal importlib\n"
        '        return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n\n'
        "    return inner()\n"
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    (inner_table,) = (c for c in outer_table.get_children() if c.get_name() == "inner")
    assert not inner_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a nonlocal-declared name must inherit the enclosing FUNCTION scope's own masked "
        f"state, not the module's — got: {sorted(discovery.executor_sites)}"
    )
    assert ("nonlocal_declared_masked_outer.py", "outer.inner") not in discovery.call_sites


def test_global_declared_name_resolves_against_module_scope_not_shadowed_param(tmp_path):
    """F1 (direction a): `global NAME` resolves every reference and
    rebinding of NAME against the MODULE scope, never the lexical-enclosing
    scope -- even when the lexical-enclosing function's OWN parameter
    shadows the same name for its own body. `outer`'s parameter `importlib`
    shadows the module import for `outer`'s own body (see
    test_function_parameter_shadowing_importlib_import_is_not_misreported),
    but `inner`'s `global importlib` skips straight past that shadow to the
    real module-level import, so this is a genuine executor construction
    site -- discarding the shadowed PARAMETER binding (as an earlier,
    unsound version of this masking pass did, by overlaying from the
    lexical parent instead of the module) silently missed it."""
    import symtable as _symtable

    rogue = tmp_path / "global_resolves_against_module_param_shadow.py"
    source = (
        "import importlib\n\n\n"
        "def outer(importlib, session, graph):\n"
        "    def inner():\n"
        "        global importlib\n"
        '        return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n\n'
        "    return inner()\n"
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    (inner_table,) = (c for c in outer_table.get_children() if c.get_name() == "inner")
    assert inner_table.lookup("importlib").is_global()
    assert not inner_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("global_resolves_against_module_param_shadow.py", "outer.inner")
    assert key in discovery.call_sites, (
        "a global declaration must resolve against the module scope even when an enclosing "
        f"function's own parameter shadows the name — found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the module-resolved global-declared importlib construction must be flagged as an "
        f"executor site — got: {sorted(discovery.executor_sites)}"
    )


def test_global_declared_name_ignores_outer_local_import_when_module_lacks_binding(tmp_path):
    """F1 (direction b): the mirror-image false positive an earlier, unsound
    version of this masking pass produced. There is NO module-level `import
    importlib` at all here; `outer` has its OWN local `import importlib`,
    and `inner` declares `global importlib`. Since `global` resolves against
    the MODULE scope -- which never bound `importlib` -- and never against
    `outer`'s local (regardless of how `outer` itself bound it), `inner`'s
    reference is to an unbound module global, not `outer`'s import; this
    must NOT register a site. An overlay that pulled from the lexical
    parent's copy instead of the module's would wrongly inherit `outer`'s
    local here and report a phantom site."""
    import symtable as _symtable

    rogue = tmp_path / "global_ignores_outer_local_no_module_binding.py"
    source = (
        "def outer(session, graph):\n"
        "    import importlib\n\n"
        "    def inner():\n"
        "        global importlib\n"
        '        return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n\n'
        "    return inner()\n"
    )
    rogue.write_text(source)

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    (inner_table,) = (c for c in outer_table.get_children() if c.get_name() == "inner")
    assert inner_table.lookup("importlib").is_global()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a global declaration with no module-level binding must not inherit an outer "
        f"function's own local import — got: {sorted(discovery.executor_sites)}"
    )
    assert (
        "global_ignores_outer_local_no_module_binding.py",
        "outer.inner",
    ) not in discovery.call_sites


def test_class_body_global_declaration_does_not_leak_into_enclosing_function(tmp_path):
    """F2: a `global`/`nonlocal` STATEMENT inside a nested class body only
    redirects lookups within that CLASS body's own namespace -- it does not
    declare the surrounding function's same-named binding at all (just as an
    ordinary assignment there binds only the class namespace; see
    test_class_body_assignment_does_not_mask_enclosing_scope_name for the
    binding side). Here `outer`'s own body has an unreachable
    `with ... as importlib` (masked per
    test_with_as_target_masks_inherited_importlib_provenance) and a nested
    class body that declares `global importlib`; if that class-body
    declaration wrongly leaked out and exempted `importlib` from `outer`'s
    own masking pass, the scanner would report an impossible site -- the
    with-as rebind inside `if False` never executes, so if `importlib` truly
    were still masked as a local the getattr call would resolve an unbound
    local, not the module import. This must NOT register a site."""
    rogue = tmp_path / "class_body_global_no_leak.py"
    source = (
        "import contextlib\n"
        "import importlib\n\n\n"
        "def outer(session, graph):\n"
        "    if False:\n"
        "        with contextlib.nullcontext() as importlib:\n"
        "            pass\n\n"
        "    class C:\n"
        "        global importlib\n\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )
    rogue.write_text(source)

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "a global declaration inside a nested class body must not un-mask the enclosing "
        f"function's own local binder — got: {sorted(discovery.executor_sites)}"
    )
    assert ("class_body_global_no_leak.py", "outer") not in discovery.call_sites


def test_global_declared_executing_binder_is_conservative_overapproximation(tmp_path):
    """F3: a declared name's own binder forms (here, a `for`-target) are
    still only ever MASK-only events in ``_replay_binding_events`` -- they
    never establish or restore provenance there, regardless of whether the
    surrounding name is declared `global`. `global importlib` means
    `importlib` is exempt from the masking pass (it is not a lexical local),
    so it keeps the MODULE-overlaid provenance straight through the `for
    importlib in [1, 2]: pass` loop even though that loop is UNCONDITIONAL
    and genuinely executes, rebinding `importlib` to a plain `int` by the
    time the getattr call runs -- at real runtime this is NOT an executor
    construction site.

    This is a DELIBERATE, documented false positive, not a bug: this
    scanner's contract is zero-false-negatives (see the module docstring) --
    a missed executor-construction site is dangerous, a spurious one only
    costs a review -- and treating a declared name's own potentially-
    executing binder as still-provenance-carrying can only ever ADD a
    reported site, never drop a real one. Eliminating this residual would
    require reasoning about whether the binder actually executes (dead/live
    code analysis), which this scanner intentionally does not attempt. A
    future change that trades this false positive for a missed real site
    must fail THIS test, not slip through review unnoticed."""
    rogue = tmp_path / "global_declared_executing_for_binder.py"
    source = (
        "import importlib\n\n\n"
        "def run(session, graph):\n"
        "    global importlib\n"
        "    for importlib in [1, 2]:\n"
        "        pass\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )
    rogue.write_text(source)

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("global_declared_executing_for_binder.py", "run")
    assert key in discovery.call_sites, (
        "a declared name's own executing binder form must still leave the declared-name "
        f"provenance in place (conservative over-approximation) — found: {sorted(discovery.call_sites)}"
    )
    assert discovery.executor_sites == {key}, (
        "the conservatively-over-approximated site must be flagged as an executor site — got: "
        f"{sorted(discovery.executor_sites)}"
    )


def test_class_body_assignment_does_not_mask_enclosing_scope_name(tmp_path):
    """An ordinary assignment inside a nested class body binds the CLASS
    namespace, never the enclosing function's -- `outer`'s `importlib` stays
    the module-level import (symtable agrees: global in `outer`, local only
    in `C`), so the construction in `outer`'s own body is REAL and must be
    reported. Collecting class-body binders as enclosing-function binding
    events would wrongly mask `importlib` here and miss the site."""
    rogue = tmp_path / "class_body_assignment_no_mask.py"
    source = (
        "import importlib\n\n\n"
        "def outer(session, graph):\n"
        "    class C:\n"
        "        importlib = object()\n\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )
    rogue.write_text(source)

    import symtable as _symtable

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    assert outer_table.lookup("importlib").is_global()
    (class_table,) = (c for c in outer_table.get_children() if c.get_name() == "C")
    assert class_table.lookup("importlib").is_local()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("class_body_assignment_no_mask.py", "outer")
    assert key in discovery.call_sites, (
        "a class-body assignment must not mask the enclosing function's module-level "
        f"import — found: {sorted(discovery.call_sites)}"
    )
    assert key in discovery.executor_sites


def test_class_body_import_provenance_visible_to_class_body_construction(tmp_path):
    """The flip side of stopping event collection at ``ClassDef``: an import
    INSIDE a class body binds the class namespace, and a construction later
    in the SAME class body can read it (class-body name resolution is
    sequential, falling through to enclosing scopes only when the name is
    not yet class-bound). ``visit_ClassDef``'s own class environment must
    carry that establishment, or dropping class-body events from the
    enclosing scope would trade the old false positive for a new missed
    real site."""
    rogue = tmp_path / "class_body_import_construct.py"
    source = (
        "def outer(session, graph):\n"
        "    class C:\n"
        "        import importlib\n"
        '        built = getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
        "    return C.built\n"
    )
    rogue.write_text(source)

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("class_body_import_construct.py", "outer.C")
    assert key in discovery.call_sites, (
        "a class-body import must stay visible to a construction in the same class "
        f"body — found: {sorted(discovery.call_sites)}"
    )
    assert key in discovery.executor_sites


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="try/except* syntax requires Python 3.11+",
)
def test_try_star_suite_global_declaration_overlays_module_scope(tmp_path):
    """A `global` declaration is a compile-time scope directive wherever it
    parses, including inside a `try`/`except*` suite -- declaration
    collection must traverse ``ast.TryStar`` exactly like ``ast.Try``, or
    the module-scope overlay is skipped and an outer parameter shadow hides
    a real module-level import (a missed real site). The fixture source is
    built as a string because `except*` does not parse on older supported
    interpreters; this test is version-gated instead."""
    rogue = tmp_path / "try_star_global_decl.py"
    source = (
        "import importlib\n\n\n"
        "def outer(importlib, session, graph):\n"
        "    def inner():\n"
        "        try:\n"
        "            global importlib\n"
        "        except* Exception:\n"
        "            pass\n"
        '        return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
        "    return inner()\n"
    )
    rogue.write_text(source)

    import symtable as _symtable

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    (inner_table,) = (c for c in outer_table.get_children() if c.get_name() == "inner")
    assert inner_table.lookup("importlib").is_global()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("try_star_global_decl.py", "outer.inner")
    assert key in discovery.call_sites, (
        "a global declaration inside a try/except* suite must overlay module-scope "
        f"provenance like any other suite — found: {sorted(discovery.call_sites)}"
    )
    assert key in discovery.executor_sites


def test_class_body_global_overlay_survives_conditional_class_rebind(tmp_path):
    """A class-body ``global`` declaration grants the class body the MODULE
    binding of the name even when an enclosing parameter shadows it -- and a
    class-body rebind of that name (here inside ``if False:``, which can
    never execute) must not erase that module baseline: the class body's
    construction really reads the module import and is a real site. The
    class replay forces events unconditional (a conditional establish that
    executes must be visible), so the module overlay is re-applied after
    replay -- dropping it would be a missed real site, not a conservative
    error."""
    rogue = tmp_path / "class_global_conditional_rebind.py"
    source = (
        "import importlib\n\n\n"
        "def outer(importlib, session, graph):\n"
        "    class C:\n"
        "        global importlib\n"
        "        if False:\n"
        "            importlib = object()\n"
        '        built = getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
        "    return C.built\n"
    )
    rogue.write_text(source)

    import symtable as _symtable

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    assert outer_table.lookup("importlib").is_local()
    (class_table,) = (c for c in outer_table.get_children() if c.get_name() == "C")
    assert class_table.lookup("importlib").is_global()

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("class_global_conditional_rebind.py", "outer.C")
    assert key in discovery.call_sites, (
        "a class-body rebind of a global-declared name must not erase the module "
        f"baseline the declaration grants — found: {sorted(discovery.call_sites)}"
    )
    assert key in discovery.executor_sites


def test_class_body_global_import_establishment_flows_to_enclosing_code(tmp_path):
    """A class body executes at class-definition time, so ``global NAME`` +
    ``import NAME`` inside it genuinely rebinds the MODULE's ``NAME`` before
    the statements after the class definition run -- the enclosing
    function's construction after the class really reads that fresh module
    binding and is a real site. The runtime check below proves the ordering
    claim with a recording fake constructor; the scanner must propagate the
    class body's own establishing events outward (add-only, never clears)."""
    rogue = tmp_path / "class_global_import_flows_out.py"
    source = (
        "def outer(session, graph):\n"
        "    class C:\n"
        "        global importlib\n"
        "        import importlib\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )
    rogue.write_text(source)

    import sys as _sys
    import types as _types

    constructed: list[tuple[object, object]] = []
    fake_flow = _types.ModuleType("lionagi.operations.flow")
    fake_flow.DependencyAwareExecutor = lambda session, graph: constructed.append((session, graph))
    namespace: dict[str, object] = {}
    with mock.patch.dict(_sys.modules, {"lionagi.operations.flow": fake_flow}):
        exec(compile(source, str(rogue), "exec"), namespace)
        namespace["outer"]("SESSION", "GRAPH")
    assert constructed == [("SESSION", "GRAPH")], (
        "the fixture must really construct through the class-established module "
        "binding at runtime — the class body executes before the return statement"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("class_global_import_flows_out.py", "outer")
    assert key in discovery.call_sites, (
        "an establishing class-global import must stay visible to the enclosing "
        f"code after the class definition — found: {sorted(discovery.call_sites)}"
    )
    assert key in discovery.executor_sites


def test_class_body_global_rebind_of_module_alias_updates_enclosing_attribution(tmp_path):
    """A class-body ``global`` rebind of a name the MODULE already aliases to
    a non-executor constructor genuinely rebinds the module name at
    class-definition time -- the enclosing function's construction after the
    class really builds the NEW target. Keeping the stale module attribution
    in any globally-resolving view would record a non-executor construction
    and omit the real executor site: a missed real site, not a conservative
    error. The runtime check proves the ordering with recording fakes for
    both constructors."""
    rogue = tmp_path / "class_global_alias_rebind.py"
    source = (
        "from fake_runtime import Graph as Ctor\n\n\n"
        "def outer(session, graph):\n"
        "    class C:\n"
        "        global Ctor\n"
        "        from fake_runtime import DependencyAwareExecutor as Ctor\n"
        "    return Ctor(session, graph)\n"
    )
    rogue.write_text(source)

    import symtable as _symtable

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    assert outer_table.lookup("Ctor").is_global()
    (class_table,) = (c for c in outer_table.get_children() if c.get_name() == "C")
    assert class_table.lookup("Ctor").is_global()

    import sys as _sys
    import types as _types

    graph_built: list[tuple[object, object]] = []
    executor_built: list[tuple[object, object]] = []
    fake_runtime = _types.ModuleType("fake_runtime")
    fake_runtime.Graph = lambda session, graph: graph_built.append((session, graph))
    fake_runtime.DependencyAwareExecutor = lambda session, graph: executor_built.append(
        (session, graph)
    )
    namespace: dict[str, object] = {}
    with mock.patch.dict(_sys.modules, {"fake_runtime": fake_runtime}):
        exec(compile(source, str(rogue), "exec"), namespace)
        namespace["outer"]("SESSION", "GRAPH")
    assert executor_built == [("SESSION", "GRAPH")] and graph_built == [], (
        "the fixture must really construct the class-rebound executor target at "
        "runtime, not the stale module alias"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("class_global_alias_rebind.py", "outer")
    assert key in discovery.executor_sites, (
        "a class-global rebind of an existing module constructor alias must "
        "update the attribution every globally-resolving view reads — found: "
        f"{sorted(discovery.executor_sites)}"
    )


def test_enclosing_local_executor_alias_survives_class_global_non_executor_rebind(tmp_path):
    """The reverse rebind direction: the enclosing function holds its OWN
    lexical-local executor alias, and the class body's ``global`` rebind
    points the MODULE name at a non-executor. The enclosing read after the
    class resolves to the function's local (the module rebind cannot touch
    it), so the real construction is still the executor -- the propagation
    must never let a non-executor establishment displace an executor
    attribution. The runtime check proves the local really wins."""
    rogue = tmp_path / "class_global_rebind_preserves_local_executor.py"
    source = (
        "def outer(session, graph):\n"
        "    from fake_runtime import DependencyAwareExecutor as Ctor\n"
        "    class C:\n"
        "        global Ctor\n"
        "        from fake_runtime import Graph as Ctor\n"
        "    return Ctor(session, graph)\n"
    )
    rogue.write_text(source)

    import symtable as _symtable

    top = _symtable.symtable(source, str(rogue), "exec")
    (outer_table,) = (c for c in top.get_children() if c.get_name() == "outer")
    assert outer_table.lookup("Ctor").is_local()
    (class_table,) = (c for c in outer_table.get_children() if c.get_name() == "C")
    assert class_table.lookup("Ctor").is_global()

    import sys as _sys
    import types as _types

    graph_built: list[tuple[object, object]] = []
    executor_built: list[tuple[object, object]] = []
    fake_runtime = _types.ModuleType("fake_runtime")
    fake_runtime.Graph = lambda session, graph: graph_built.append((session, graph))
    fake_runtime.DependencyAwareExecutor = lambda session, graph: executor_built.append(
        (session, graph)
    )
    namespace: dict[str, object] = {}
    with mock.patch.dict(_sys.modules, {"fake_runtime": fake_runtime}):
        exec(compile(source, str(rogue), "exec"), namespace)
        namespace["outer"]("SESSION", "GRAPH")
    assert executor_built == [("SESSION", "GRAPH")] and graph_built == [], (
        "the fixture must really construct through the function's own local "
        "executor alias — the class-global rebind targets the module, not it"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("class_global_rebind_preserves_local_executor.py", "outer")
    assert key in discovery.executor_sites, (
        "a non-executor class-global establishment must never displace an "
        f"executor attribution — found: {sorted(discovery.executor_sites)}"
    )


def test_class_global_executor_rebind_over_local_non_executor_alias_errs_reportward(tmp_path):
    """Conscious over-approximation pin for the remaining rebind cell: the
    enclosing function's OWN lexical-local alias is a non-executor, and the
    class-global rebind establishes an executor. The runtime proof shows the
    real construction is the local non-executor (the module rebind cannot
    touch a lexical local), but the scanner still reports an executor
    candidate here: distinguishing this cell would require per-view
    local/closure/module resolution whose mistakes hide real sites, so the
    alias propagation deliberately errs toward reporting an executor. A
    spurious candidate costs review attention; the reverse error violates
    the zero-false-negative contract."""
    rogue = tmp_path / "class_global_executor_rebind_over_local.py"
    source = (
        "def outer(session, graph):\n"
        "    from fake_runtime import Graph as Ctor\n"
        "    class C:\n"
        "        global Ctor\n"
        "        from fake_runtime import DependencyAwareExecutor as Ctor\n"
        "    return Ctor(session, graph)\n"
    )
    rogue.write_text(source)

    import sys as _sys
    import types as _types

    graph_built: list[tuple[object, object]] = []
    executor_built: list[tuple[object, object]] = []
    fake_runtime = _types.ModuleType("fake_runtime")
    fake_runtime.Graph = lambda session, graph: graph_built.append((session, graph))
    fake_runtime.DependencyAwareExecutor = lambda session, graph: executor_built.append(
        (session, graph)
    )
    namespace: dict[str, object] = {}
    with mock.patch.dict(_sys.modules, {"fake_runtime": fake_runtime}):
        exec(compile(source, str(rogue), "exec"), namespace)
        namespace["outer"]("SESSION", "GRAPH")
    assert graph_built == [("SESSION", "GRAPH")] and executor_built == [], (
        "the fixture must really construct the local non-executor alias — this "
        "test pins the scanner's deliberate reportward error for that cell"
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("class_global_executor_rebind_over_local.py", "outer")
    assert key in discovery.executor_sites, (
        "the alias propagation must err reportward in this cell (spurious "
        "executor candidate, never a hidden one) — found: "
        f"{sorted(discovery.executor_sites)}"
    )


def test_annotated_assignment_establishes_provenance_like_plain_assignment(tmp_path):
    """An annotated assignment with a value binds exactly like a plain
    assignment -- ``flow_mod: object = importlib.import_module(...)`` gives
    ``flow_mod`` flow-module provenance, and the construction through it is
    a real site. Missing this establish path would be a missed real site,
    not a conservative error."""
    rogue = tmp_path / "annassign_establishes.py"
    source = (
        "import importlib\n\n\n"
        "def run(session, graph):\n"
        '    flow_mod: object = importlib.import_module("lionagi.operations.flow")\n'
        '    return getattr(flow_mod, "DependencyAwareExecutor")(session, graph)\n'
    )
    rogue.write_text(source)

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    key = ("annassign_establishes.py", "run")
    assert key in discovery.call_sites, (
        "an annotated assignment establishing flow-module provenance must be "
        f"tracked like a plain assignment — found: {sorted(discovery.call_sites)}"
    )
    assert key in discovery.executor_sites


def test_annotated_assignment_masks_inherited_provenance_like_plain_assignment(tmp_path):
    """An annotated assignment to something unrecognized makes the name a
    function-local rebound to junk -- inherited module provenance must be
    masked exactly as a plain assignment masks it, and a BARE annotation
    (``name: object`` with no value) still makes the name a lexical local of
    the whole scope (reading it raises UnboundLocalError at runtime), so
    both shapes must produce no site."""
    rogue = tmp_path / "annassign_masks.py"
    rogue.write_text(
        "import importlib\n\n\n"
        "def rebound(session, graph):\n"
        "    importlib: object = object()\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n\n\n'
        "def annotated_only(session, graph):\n"
        "    importlib: object\n"
        '    return getattr(importlib.import_module("lionagi.operations.flow"), '
        '"DependencyAwareExecutor")(session, graph)\n'
    )

    discovery = discover_call_and_construct_sites(tmp_path, base=tmp_path)

    assert discovery.executor_sites == set(), (
        "annotated assignments and bare annotations are lexical locals and must "
        f"mask inherited provenance — got: {sorted(discovery.executor_sites)}"
    )


def test_every_statement_class_is_handled_or_consciously_excluded():
    """Exhaustiveness pin for the statement-level traversal: every concrete
    ``ast.stmt`` subclass the running interpreter defines must be either
    HANDLED by the scope machinery (declaration collection, binding-event
    collection, control-flow descent, or a deliberate scope stop) or listed
    here as CONSCIOUSLY EXCLUDED with a direction argument. A statement
    class added by a future Python version is silently untraversed by
    default -- exactly how a new compound-statement form would otherwise
    slip past both collectors unnoticed -- so its absence from these sets
    must fail THIS test and force an explicit disposition."""
    declaration_stmts = {ast.Global, ast.Nonlocal}
    event_stmts = {
        ast.Import,
        ast.ImportFrom,
        ast.Assign,
        ast.AnnAssign,
        ast.AugAssign,
        ast.Delete,
    }
    descent_stmts = {
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.With,
        ast.AsyncWith,
        ast.Match,
        *_TRY_STMT_TYPES,
    }
    scope_stops = {ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef}
    # No statement body and no name binding at statement level; expression-
    # level binders inside them (walrus, comprehensions) are the documented
    # residual imprecision.
    excluded_inert = {
        ast.Return,
        ast.Expr,
        ast.Pass,
        ast.Break,
        ast.Continue,
        ast.Raise,
        ast.Assert,
    }
    # Binders whose omission errs only false-positive-safe: the bound value
    # can never carry flow-module provenance (a def/class binds a function/
    # class object, a type-alias statement binds a TypeAliasType), so an
    # unmasked same-named inherited provenance at worst ADDS a spurious
    # site -- never hides a real one. FunctionDef/AsyncFunctionDef/ClassDef
    # appear in scope_stops for traversal; their NAME bindings fall in this
    # class too.
    excluded_fp_safe_binders = {ast.TypeAlias} if hasattr(ast, "TypeAlias") else set()
    accounted = (
        declaration_stmts
        | event_stmts
        | descent_stmts
        | scope_stops
        | excluded_inert
        | excluded_fp_safe_binders
    )
    concrete_stmt_classes = {
        cls
        for cls in vars(ast).values()
        if isinstance(cls, type) and issubclass(cls, ast.stmt) and cls is not ast.stmt
    }
    unaccounted = concrete_stmt_classes - accounted
    assert not unaccounted, (
        "statement classes with no explicit disposition in the scope machinery "
        "(handle them in _scope_declared_names/_collect_scope_binding_events or "
        "consciously exclude them here with a direction argument): "
        f"{sorted(cls.__name__ for cls in unaccounted)}"
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
    from lionagi.engines import PlanningEngine
    from tests.cli.orchestrate.test_flow_phases import _FakeBranch, _make_env

    env = _make_env(tmp_path)
    env.exchange = None
    env.session.observer = mock.MagicMock()

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

    async def run_dag(graph, **kwargs):
        return await env.session.flow(graph, verbose=kwargs.get("verbose", False))

    engine_run = mock.MagicMock()
    engine_run.run_dag = AsyncMock(side_effect=run_dag)

    with (
        mock.patch.object(fanout_mod, "plan", fake_plan),
        mock.patch.object(fanout_mod, "available_roles", fake_available_roles),
        mock.patch.object(fanout_mod, "build_worker_branch", fake_build_worker_branch),
        mock.patch.object(fanout_mod, "finalize_orchestration", fake_finalize),
        mock.patch.object(PlanningEngine, "new_run", return_value=engine_run),
    ):
        await fanout_mod._run_fanout_inner("codex/gpt-5.5", "do the batch", env=env, num_workers=1)

    engine_run.run_dag.assert_awaited_once()
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
    from lionagi import Branch, Session
    from lionagi.cli._runs import RunDir
    from lionagi.cli.orchestrate._orchestration import OrchestrationEnv

    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    run = RunDir(
        run_id="run-test-1",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    run.ensure_state_dirs()
    run.ensure_artifact_root()
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
