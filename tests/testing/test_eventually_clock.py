# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Polling deadlines ignore adjustments to the process wall clock."""

from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest


@pytest.mark.asyncio
async def test_eventually_succeeds_despite_forward_wall_clock_jump(monkeypatch):
    from lionagi.testing import helpers

    origin = anyio.current_time()
    wall_reads = 0

    def wall_time():
        nonlocal wall_reads
        wall_reads += 1
        return origin + (1000 if wall_reads == 1 else 1_000_000)

    # Replace only this module's time reference, preserving the actual
    # event-loop clock and the test runner's clock.
    monkeypatch.setattr(helpers, "time", SimpleNamespace(time=wall_time))
    checks = 0

    def eventually_true():
        nonlocal checks
        checks += 1
        return checks == 2

    with anyio.fail_after(2):
        await helpers.AsyncTestHelpers.assert_eventually(eventually_true, timeout=1, interval=0)
    assert checks == 2


@pytest.mark.asyncio
async def test_eventually_times_out_despite_backward_wall_clock_jump(monkeypatch):
    from lionagi.testing import helpers

    origin = anyio.current_time()
    wall_reads = 0

    def wall_time():
        nonlocal wall_reads
        wall_reads += 1
        return origin + (1000 if wall_reads == 1 else 900)

    monkeypatch.setattr(helpers, "time", SimpleNamespace(time=wall_time))
    checks = 0

    def never_true():
        nonlocal checks
        checks += 1
        return False

    started = anyio.current_time()
    with anyio.fail_after(2):
        with pytest.raises(AssertionError, match="condition stayed false"):
            await helpers.AsyncTestHelpers.assert_eventually(
                never_true, timeout=0.02, interval=0.001, error_message="condition stayed false"
            )
    assert checks > 0
    assert anyio.current_time() - started >= 0.02
