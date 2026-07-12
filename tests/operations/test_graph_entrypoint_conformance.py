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

Registering a location in the manifest is necessary but not sufficient: any
row that names an ``expected_target`` must also name a ``delegation_test`` —
the exact pytest node id of the test that actually asserts the delegation
(call count, argument identity, or a mocked target being reached) — and any
row with ``persistence="required"`` must name a ``persistence_evidence`` node
id backed by a real StateDB write. Both are validated against real source
(not just checked for non-emptiness), so a stale or nonexistent reference
fails the suite instead of reading as coverage.
"""

from __future__ import annotations

import argparse
import ast
import inspect
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
        reason="CLI fan-out; opens/binds live-persist StateDB state before submitting the graph",
        persistence_evidence=(
            "tests/cli/orchestrate/test_live_persist.py::"
            "test_start_creates_session_and_registers_hook_on_orc_branch"
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
        reason="CLI flow execution phase; drives the planning engine's run_dag over the built DAG",
        persistence_evidence=(
            "tests/cli/orchestrate/test_live_persist.py::"
            "test_start_creates_session_and_registers_hook_on_orc_branch"
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
        expected_target="EngineRun.run_dag",
        persistence="inherited",
        reason="registers the 'engine' operation consumed by run-workflow-def; the DAG hop this node "
        "takes is covered by planning-engine-run and engine-run-dag",
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


class _SinkVisitor(ast.NodeVisitor):
    """Attributes qualified `.flow`/`.flow_stream`/`.run_dag` calls, bare
    calls to a locally-imported kernel function, and
    OperationGraphBuilder/executor/Graph construction calls to the innermost
    enclosing function or method (dotted "Class.method" or "function")."""

    def __init__(self, kernel_names: set[str] = frozenset()) -> None:
        self._stack: list[str] = []
        self._kernel_names = kernel_names
        self.hits: dict[str, set[str]] = {}
        self.linenos: dict[str, int] = {}
        self.executor_hits: set[str] = set()

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

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

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
        elif isinstance(func, ast.Attribute) and func.attr in _CONSTRUCTOR_SINK_NAMES:
            self._record(
                f"constructs {func.attr}()",
                node.lineno,
                is_executor=func.attr in _EXECUTOR_CONSTRUCTOR_NAMES,
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
        visitor = _SinkVisitor(kernel_names)
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
        visitor = _SinkVisitor(kernel_names)
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
    fails — independent of the name-based heuristic above."""
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
