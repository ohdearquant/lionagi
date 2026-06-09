# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the anyio.NoEventLoopError fix (#1082).

Root cause: ``lionagi/cli/agent.py`` called ``anyio.get_cancelled_exc_class()``
inside an ``except BaseException`` block that executes *after* ``run_async``
has torn down the event loop.  ``anyio.get_cancelled_exc_class()`` queries the
running backend and raises ``NoEventLoopError`` when there is none.

Fix: ``cache_cancelled_exc_class()`` captures the class from inside a live loop;
``cancelled_exc_classes()`` returns the cached tuple (or a safe asyncio fallback)
so it never touches anyio after the loop exits.
"""

from __future__ import annotations

import asyncio
import importlib

import anyio
import pytest

# ---------------------------------------------------------------------------
# Unit tests for lionagi.ln.concurrency.errors
# ---------------------------------------------------------------------------


class TestCacheAndReaderFunctions:
    """Verify the new cache helpers in isolation."""

    def setup_method(self) -> None:
        """Reset module-level cache before each test."""
        import lionagi.ln.concurrency.errors as errors_mod

        errors_mod._CANCELLED_EXC_CLASS = None

    def test_cancelled_exc_classes_returns_asyncio_fallback_when_no_cache(
        self,
    ) -> None:
        from lionagi.ln.concurrency.errors import cancelled_exc_classes

        result = cancelled_exc_classes()
        assert asyncio.CancelledError in result
        # Must never raise NoEventLoopError — this is the core invariant.

    def test_cancelled_exc_classes_never_calls_anyio_after_loop_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anyio as _anyio

        import lionagi.ln.concurrency.errors as errors_mod

        errors_mod._CANCELLED_EXC_CLASS = None

        # Poison anyio.get_cancelled_exc_class so any call would explode.
        def _bad_get_cancelled() -> None:
            raise RuntimeError("anyio must not be called after loop exit")

        monkeypatch.setattr(_anyio, "get_cancelled_exc_class", _bad_get_cancelled)

        from lionagi.ln.concurrency.errors import cancelled_exc_classes

        # Must NOT propagate the RuntimeError — fallback takes over.
        result = cancelled_exc_classes()
        assert asyncio.CancelledError in result

    def test_cache_cancelled_exc_class_populates_cache(self) -> None:
        import lionagi.ln.concurrency.errors as errors_mod

        errors_mod._CANCELLED_EXC_CLASS = None

        async def _inner() -> None:
            from lionagi.ln.concurrency.errors import cache_cancelled_exc_class

            cache_cancelled_exc_class()

        asyncio.run(_inner())

        assert errors_mod._CANCELLED_EXC_CLASS is not None
        assert asyncio.CancelledError in errors_mod._CANCELLED_EXC_CLASS

    def test_cache_is_idempotent(self) -> None:
        import lionagi.ln.concurrency.errors as errors_mod

        errors_mod._CANCELLED_EXC_CLASS = None

        async def _inner() -> None:
            from lionagi.ln.concurrency.errors import cache_cancelled_exc_class

            cache_cancelled_exc_class()
            first = errors_mod._CANCELLED_EXC_CLASS

            cache_cancelled_exc_class()
            second = errors_mod._CANCELLED_EXC_CLASS

            assert first is second

        asyncio.run(_inner())

    def test_cancelled_exc_classes_returns_cached_after_loop_exit(self) -> None:
        import lionagi.ln.concurrency.errors as errors_mod

        errors_mod._CANCELLED_EXC_CLASS = None

        async def _inner() -> None:
            from lionagi.ln.concurrency.errors import cache_cancelled_exc_class

            cache_cancelled_exc_class()

        asyncio.run(_inner())

        # Simulate: loop is gone, anyio would raise NoEventLoopError if called.
        # But cancelled_exc_classes() should use the cache.
        from lionagi.ln.concurrency.errors import cancelled_exc_classes

        result = cancelled_exc_classes()
        assert asyncio.CancelledError in result


# ---------------------------------------------------------------------------
# Integration test: simulate the run_agent error path post-loop-exit
# ---------------------------------------------------------------------------


class TestRunAgentCancelledExcPath:
    def setup_method(self) -> None:
        """Reset module-level cache before each test."""
        import lionagi.ln.concurrency.errors as errors_mod

        errors_mod._CANCELLED_EXC_CLASS = None

    def test_no_noeventloouperror_when_classifying_after_loop_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anyio as _anyio

        import lionagi.ln.concurrency.errors as errors_mod
        from lionagi.ln.concurrency import run_async

        # Phase 1: run inside a loop and populate the cache.
        async def _populate_cache() -> None:
            from lionagi.ln.concurrency.errors import cache_cancelled_exc_class

            cache_cancelled_exc_class()

        run_async(_populate_cache())

        # Phase 2: loop is now closed. Poison anyio to ensure it's not called.
        def _raise_no_loop() -> type[BaseException]:
            raise anyio.ClosedResourceError("no event loop (simulated)")

        monkeypatch.setattr(_anyio, "get_cancelled_exc_class", _raise_no_loop)

        # This is the critical assertion: the old code would hit the monkeypatched
        # function and raise; the new code uses the cache and must succeed.
        from lionagi.ln.concurrency.errors import cancelled_exc_classes

        exc = asyncio.CancelledError()
        # Must not raise, and must classify CancelledError correctly.
        assert isinstance(exc, cancelled_exc_classes())

    def test_run_agent_uses_cancelled_exc_classes_not_get_cancelled_exc_class(
        self,
    ) -> None:
        """Verify the *sync* run_agent() no longer calls get_cancelled_exc_class.

        There are two ``except BaseException`` handlers in agent.py:
        - One inside ``_run_agent`` (async) — runs inside the event loop, safe.
        - One inside ``run_agent`` (sync) — runs AFTER run_async() exits the loop;
          calling anyio.get_cancelled_exc_class() there raises NoEventLoopError.

        This test checks only the handler belonging to the *sync* ``run_agent``
        function definition, so the safe in-loop usage in ``_run_agent`` does not
        trigger a false positive.
        """
        import ast
        import pathlib

        agent_path = pathlib.Path(__file__).parent.parent.parent / "lionagi" / "cli" / "agent.py"
        source = agent_path.read_text()
        tree = ast.parse(source)

        # Find the *sync* ``run_agent`` function definition (not ``_run_agent``).
        run_agent_node: ast.FunctionDef | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_agent":
                run_agent_node = node
                break

        assert run_agent_node is not None, "run_agent function not found in agent.py"

        # Walk only the body of run_agent for BaseException handlers.
        for node in ast.walk(run_agent_node):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                continue
            handler_type = ast.unparse(node.type)
            if "BaseException" not in handler_type:
                continue
            # Collect all function calls within this handler body.
            calls_in_handler = [
                ast.unparse(n)
                for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
                if isinstance(n, ast.Call)
            ]
            for call_text in calls_in_handler:
                assert "get_cancelled_exc_class" not in call_text, (
                    f"run_agent() BaseException handler still calls get_cancelled_exc_class: "
                    f"{call_text!r} — this triggers NoEventLoopError after loop exit (#1082)"
                )
