# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for lionagi CLI orchestration paths.

These tests exercise the ACTUAL integration between subsystems to catch
regressions that unit tests miss. They use real Session/Branch/HookBus
instances — no mocking of the subsystems under test.

Bugs caught by this suite:
- cancelled_exc_classes called outside event loop (NoEventLoopError)
- aggregation_sources/aggregation_count placed in parameters instead of metadata
- _on_bus_spawn was sync but observer.emit() awaits handler returns
- HookBus not wired into Session/Branch on include_branches
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

# ── Test 1: cancelled_exc_classes safe to call outside loop ──────────────────


def test_cancelled_exc_safe_outside_loop():
    """Calling cancelled_exc_classes() from sync context must not raise.

    The pre-fix code called get_cancelled_exc_class() (anyio) which needs a
    running event loop and raises NoEventLoopError outside one.
    cancelled_exc_classes() uses the populated cache or falls back to the
    asyncio baseline — never calls anyio in a sync context.
    """
    from lionagi.ln.concurrency.errors import cancelled_exc_classes

    result = cancelled_exc_classes()

    assert isinstance(result, tuple)
    assert len(result) >= 1
    # asyncio.CancelledError must always be in the tuple (the safe baseline)
    assert asyncio.CancelledError in result


def test_is_cancelled_works_with_cancelled_error():
    """is_cancelled() must recognise asyncio.CancelledError outside a loop."""
    from lionagi.ln.concurrency.errors import is_cancelled

    exc = asyncio.CancelledError()
    assert is_cancelled(exc) is True


def test_is_cancelled_false_for_non_cancel():
    """is_cancelled() must return False for non-cancellation exceptions."""
    from lionagi.ln.concurrency.errors import is_cancelled

    assert is_cancelled(ValueError("not a cancel")) is False
    assert is_cancelled(RuntimeError()) is False


# ── Test 2: cache_cancelled_exc_class populates the cache ────────────────────


async def test_cancelled_exc_cache_populated_after_explicit_cache():
    """cache_cancelled_exc_class() inside an event loop populates the module cache.

    After caching, cancelled_exc_classes() returns the anyio backend type
    (which equals asyncio.CancelledError for the asyncio backend) plus
    asyncio.CancelledError, so the result is never empty.
    """
    from lionagi.ln.concurrency import errors as _err_mod
    from lionagi.ln.concurrency.errors import cache_cancelled_exc_class, cancelled_exc_classes

    # Reset module-level cache to simulate first call
    original = _err_mod._CANCELLED_EXC_CLASS
    _err_mod._CANCELLED_EXC_CLASS = None
    try:
        # Must be called from inside an event loop
        cache_cancelled_exc_class()
        result = cancelled_exc_classes()
        assert isinstance(result, tuple)
        assert len(result) >= 1
        assert asyncio.CancelledError in result
    finally:
        # Restore original state
        _err_mod._CANCELLED_EXC_CLASS = original


# ── Test 3: aggregation_sources in metadata, NOT in parameters ───────────────


def test_aggregation_params_in_metadata_not_parameters():
    """build_fanout_graph must place aggregation keys in metadata, not parameters.

    The pre-fix code put aggregation_sources and aggregation_count into the
    parameters dict. Those get spread as **kwargs into branch.operate(), causing
    TypeError: operate() got unexpected keyword argument 'aggregation_sources'.
    """
    from lionagi.casts.emission import TaskAssignment
    from lionagi.orchestration.patterns import build_fanout_graph
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    session = Session()

    # Create two role branches and register them
    worker_a = Branch(name="worker_a")
    worker_b = Branch(name="worker_b")
    synth = Branch(name="synth")
    session.include_branches(worker_a)
    session.include_branches(worker_b)
    session.include_branches(synth)

    roles = {"worker_a": worker_a, "worker_b": worker_b, "synth": synth}

    assignments = [
        TaskAssignment(task="Do task A", assignee="worker_a"),
        TaskAssignment(task="Do task B", assignee="worker_b"),
    ]

    graph, worker_ids = build_fanout_graph(
        session,
        assignments,
        roles,
        synthesis_role="synth",
    )

    # Find the synthesis node (non-worker)
    from lionagi.operations.node import Operation

    synth_node = None
    for node in graph.internal_nodes.values():
        if isinstance(node, Operation) and node.metadata.get("aggregation"):
            synth_node = node
            break

    assert synth_node is not None, "No synthesis node found in graph"

    # aggregation keys must be in metadata
    assert "aggregation_sources" in synth_node.metadata, (
        "aggregation_sources must be in Operation.metadata, not parameters"
    )
    assert "aggregation_count" in synth_node.metadata, (
        "aggregation_count must be in Operation.metadata, not parameters"
    )
    assert len(synth_node.metadata["aggregation_sources"]) == 2

    # parameters must only contain instruction — NOT aggregation kwargs
    params = synth_node.parameters
    if isinstance(params, dict):
        assert "aggregation_sources" not in params, (
            "aggregation_sources must NOT be in parameters (causes TypeError in operate())"
        )
        assert "aggregation_count" not in params, (
            "aggregation_count must NOT be in parameters (causes TypeError in operate())"
        )
        assert "instruction" in params


# ── Test 4: _on_bus_spawn is async ───────────────────────────────────────────


def test_on_bus_spawn_is_async():
    """ReactiveExecutor._on_bus_spawn must be an async function.

    The pre-fix code had a sync _on_bus_spawn. The observer's emit() calls
    handler(matched, ctx) and then inspects the return for isawaitable —
    a sync handler returning None is fine in that path, but the handler is
    also registered with session.observe(), whose emit gathers coros. A sync
    handler that does side effects (like calling _inject_request) appears to
    work but actually executes in the wrong concurrency context, creating
    subtle ordering bugs. The fix makes it async so await semantics are clear.
    """
    from lionagi.operations.flow import ReactiveExecutor

    assert inspect.iscoroutinefunction(ReactiveExecutor._on_bus_spawn), (
        "ReactiveExecutor._on_bus_spawn must be a coroutine function (async def)"
    )


# ── Test 5: Session has hooks property returning HookBus ─────────────────────


def test_session_has_hooks_property():
    """Session.hooks must lazily return a HookBus bound to session.observer."""
    from lionagi.hooks import HookBus
    from lionagi.session.session import Session

    session = Session()

    # hooks is a lazy property — accessing it creates the bus
    bus = session.hooks

    assert isinstance(bus, HookBus), f"session.hooks must return HookBus, got {type(bus)}"
    assert session._hooks is not None, "session._hooks must be populated after first access"
    assert session._hooks is bus, "session._hooks and session.hooks must be the same object"

    # The bus must be bound to the session's observer
    assert bus._observer is session.observer, (
        "HookBus._observer must be session.observer (ADR-0076)"
    )


def test_session_hooks_identity_stable():
    """Accessing session.hooks multiple times returns the same bus."""
    from lionagi.hooks import HookBus
    from lionagi.session.session import Session

    session = Session()
    bus1 = session.hooks
    bus2 = session.hooks

    assert bus1 is bus2, "session.hooks must return the same HookBus instance on repeated access"


# ── Test 6: Branch gets hooks from session via include_branches ───────────────


def test_branch_gets_hooks_from_session():
    """include_branches must propagate session._hooks to each added branch.

    Pre-fix: Session.include_branches didn't check self._hooks; only _observer
    was propagated. Branches therefore had no hooks bus and couldn't fire
    hook-point events.
    """
    from lionagi.hooks import HookBus
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    session = Session()
    # Initialise the hook bus on the session BEFORE adding the branch
    _ = session.hooks  # forces _hooks creation

    new_branch = Branch(name="test_branch")
    session.include_branches(new_branch)

    assert new_branch._hooks is not None, "Branch._hooks must be set after include_branches"
    assert new_branch._hooks is session._hooks, (
        "branch._hooks must be the same object as session._hooks"
    )
    assert isinstance(new_branch._hooks, HookBus)


def test_branch_gets_hooks_when_added_after_bus_init():
    """Branches added after hooks init must receive the already-created bus."""
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    session = Session()
    bus = session.hooks  # create bus first

    b1 = Branch(name="b1")
    b2 = Branch(name="b2")
    session.include_branches(b1)
    session.include_branches(b2)

    assert b1._hooks is bus
    assert b2._hooks is bus


# ── Test 7: executor uses _observer private attr, not .observer property ──────


def test_reactive_executor_uses_private_observer_attr():
    """ReactiveExecutor.execute() must access session._observer (the private
    PrivateAttr), NOT session.observer (the lazy property).

    The difference matters: ``session.observer`` (property) creates a
    SessionObserver unconditionally, which has side-effects (wires the exchange,
    attaches to the bus) even when no reactive spawn is needed. The executor
    uses ``getattr(self.session, '_observer', None)`` so it only subscribes when
    an observer already exists — no implicit observer creation.

    This test confirms the source-level contract.
    """
    source = inspect.getsource(
        __import__(
            "lionagi.operations.flow", fromlist=["ReactiveExecutor"]
        ).ReactiveExecutor.execute
    )

    # Must use the private attribute access pattern
    assert '"_observer"' in source or "'_observer'" in source, (
        "ReactiveExecutor.execute() must access session._observer via getattr "
        "(the private PrivateAttr), not session.observer (the lazy property)"
    )

    # Must NOT call the property directly (self.session.observer without getattr)
    # Allow 'self.session.observer' only in comments, not as a bare attribute access
    non_comment_lines = [
        (i + 1, line)
        for i, line in enumerate(source.splitlines())
        if "session.observer" in line and not line.strip().startswith("#") and "getattr" not in line
    ]
    assert not non_comment_lines, (
        f"ReactiveExecutor.execute() must not call session.observer (lazy property). "
        f"Found bare access at lines: {non_comment_lines}"
    )


# ── Test 8: _wait_for_dependencies reads aggregation_sources from metadata ────


def test_flow_aggregation_wait_reads_metadata():
    """DependencyAwareExecutor._wait_for_dependencies reads aggregation_sources
    from operation.metadata (not operation.parameters).

    If the executor read from parameters, it would find nothing (since
    build_fanout_graph stores them in metadata) and skip the aggregation wait,
    causing the synthesis to fire before workers complete.
    """
    from lionagi.operations.flow import DependencyAwareExecutor

    # Inspect the source of _wait_for_dependencies
    source = inspect.getsource(DependencyAwareExecutor._wait_for_dependencies)

    # Must reference operation.metadata.get("aggregation_sources")
    # (not operation.parameters.get or parameters["aggregation_sources"])
    assert (
        'metadata.get("aggregation_sources"' in source
        or "metadata.get('aggregation_sources'" in source
    ), (
        "_wait_for_dependencies must read aggregation_sources from operation.metadata, "
        "not operation.parameters"
    )

    # Must NOT read aggregation_sources from parameters
    # (a false positive would be parameters having the key — already caught by test 3)
    assert 'parameters.get("aggregation_sources"' not in source, (
        "_wait_for_dependencies must not read aggregation_sources from operation.parameters"
    )


# ── Test 9: CLI error handler imports cancelled_exc_classes (not get_cancelled_exc_class) ──


def test_error_handler_uses_cached_exc_class():
    """The CLI orchestrate module must import cancelled_exc_classes, not
    get_cancelled_exc_class.

    get_cancelled_exc_class() (anyio) requires a running event loop and raises
    NoEventLoopError in exception handlers that run after the loop exits.
    cancelled_exc_classes() is the loop-safe cached variant.
    """
    import ast
    from pathlib import Path

    orchestrate_init = Path(__file__).parent.parent.parent / "lionagi/cli/orchestrate/__init__.py"
    source = orchestrate_init.read_text()

    tree = ast.parse(source)

    # Collect all names imported from lionagi.ln.concurrency.errors
    imported_from_errors: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "concurrency" in module and "errors" in module:
                for alias in node.names:
                    imported_from_errors.add(alias.asname or alias.name)

    assert "cancelled_exc_classes" in imported_from_errors, (
        "CLI orchestrate __init__ must import cancelled_exc_classes "
        "(the loop-safe cached variant), not get_cancelled_exc_class"
    )

    # Confirm the dangerous variant is NOT directly called (it may be imported
    # elsewhere but must not appear as a bare call in this file)
    # We check for the pattern in source as a belt-and-suspenders guard.
    # Allow the import of the name itself for completeness testing, but not a call.
    lines_with_get = [
        (i + 1, line)
        for i, line in enumerate(source.splitlines())
        if "get_cancelled_exc_class()" in line and not line.strip().startswith("#")
    ]
    assert not lines_with_get, (
        f"get_cancelled_exc_class() is called directly in orchestrate/__init__.py at "
        f"lines {lines_with_get} — use cancelled_exc_classes() instead"
    )


# ── Test 10: HookBus lifecycle — default hooks are registered ────────────────


def test_hook_bus_lifecycle_integration():
    """Session.hooks returns a properly-wired bus with all DEFAULT_HOOKS registered.

    This is an end-to-end check of the bus lifecycle:
    1. session.hooks creates the bus
    2. build_session_bus wires DEFAULT_HOOKS
    3. The bus is bound to the observer
    4. Default hook points all have at least one handler
    """
    from lionagi.hooks import DEFAULT_HOOKS, HookBus, HookPoint
    from lionagi.session.session import Session

    session = Session()
    bus = session.hooks

    assert isinstance(bus, HookBus)

    # All four default hook points must have handlers registered
    for point in (
        HookPoint.SESSION_START,
        HookPoint.SESSION_END,
        HookPoint.MESSAGE_ADD,
        HookPoint.BRANCH_CREATE,
    ):
        handlers = bus.handlers_for(point)
        assert len(handlers) >= 1, (
            f"HookPoint.{point.name} must have at least one default handler registered; "
            f"got {handlers!r}"
        )
        # The handlers must come from DEFAULT_HOOKS
        default_handlers = DEFAULT_HOOKS[point]
        for h in default_handlers:
            assert h in handlers, (
                f"Default handler {h.__name__!r} missing from bus for {point.name}"
            )

    # Bus must be bound to the session observer (ADR-0076)
    assert bus._observer is session.observer


def test_hook_bus_handlers_for_returns_shallow_copy():
    """handlers_for() must return a copy so callers cannot mutate the bus state."""
    from lionagi.hooks import HookPoint
    from lionagi.session.session import Session

    session = Session()
    bus = session.hooks

    handlers = bus.handlers_for(HookPoint.SESSION_START)
    original_len = len(handlers)

    # Mutating the returned list must not affect the bus
    handlers.clear()
    assert len(bus.handlers_for(HookPoint.SESSION_START)) == original_len, (
        "handlers_for() must return a shallow copy — mutating it must not affect the bus"
    )


# ── Test 11: aggregation metadata survives graph round-trip ──────────────────


def test_aggregation_metadata_survives_without_parameters_leak():
    """Aggregation node must have exactly the keys expected in metadata and
    exactly 'instruction' in parameters — no cross-contamination.

    This directly verifies the regression: aggregation_sources must NOT appear
    in the parameters dict that gets spread into branch.operate(**params).
    """
    from lionagi.casts.emission import TaskAssignment
    from lionagi.operations.node import Operation
    from lionagi.orchestration.patterns import build_fanout_graph
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    session = Session()
    roles = {}
    for name in ("analyst", "researcher", "synthesizer"):
        b = Branch(name=name)
        session.include_branches(b)
        roles[name] = b

    assignments = [
        TaskAssignment(task="Analyse market data", assignee="analyst"),
        TaskAssignment(task="Research competitors", assignee="researcher"),
    ]

    graph, worker_ids = build_fanout_graph(
        session, assignments, roles, synthesis_role="synthesizer"
    )

    synth_node = next(
        (
            n
            for n in graph.internal_nodes.values()
            if isinstance(n, Operation) and n.metadata.get("aggregation")
        ),
        None,
    )
    assert synth_node is not None

    # Metadata checks
    meta = synth_node.metadata
    assert meta["aggregation"] is True
    assert meta["aggregation_count"] == 2
    assert len(meta["aggregation_sources"]) == 2
    # Sources are stored as str(w.id) — compare as strings
    for wid in worker_ids:
        assert str(wid) in meta["aggregation_sources"], (
            f"Worker id {wid!r} missing from aggregation_sources"
        )

    # Parameters checks — only instruction, no aggregation leakage
    params = synth_node.parameters
    assert isinstance(params, dict)
    assert set(params.keys()) == {"instruction"}, (
        f"Synthesis node parameters must only contain 'instruction'; "
        f"got extra keys: {set(params.keys()) - {'instruction'}}"
    )
