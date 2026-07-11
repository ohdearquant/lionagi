# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the teardown_orchestration_persist deprecation wrapper."""

from __future__ import annotations

import inspect
import warnings

from lionagi.cli._runs import teardown_orchestration_persist, teardown_persist


class TestTeardownOrchestrationPersistDeprecation:
    def test_is_async(self):
        assert inspect.iscoroutinefunction(teardown_orchestration_persist)

    async def test_warns_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await teardown_orchestration_persist(None, status="completed")

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "teardown_orchestration_persist" in str(deprecations[0].message)
        assert "teardown_persist" in str(deprecations[0].message)

    async def test_stacklevel_identifies_caller(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await teardown_orchestration_persist(None, status="completed")  # this exact line

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert deprecations[0].filename == __file__

    async def test_delegates_unchanged_to_teardown_persist(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            wrapped_result = await teardown_orchestration_persist(None, status="failed")

        direct_result = await teardown_persist(None, status="failed")

        assert wrapped_result == direct_result == "failed"

    async def test_teardown_persist_itself_does_not_warn(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await teardown_persist(None, status="completed")

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 0
