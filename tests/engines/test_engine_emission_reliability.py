# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lionagi.engines.engine import Engine
from lionagi.engines.review import ReviewEngine
from lionagi.providers._provider_errors import ProviderContextError


class _StubEngine(Engine):
    async def _run(self, run, *args, **kwargs):  # pragma: no cover
        return ""


class _NearLimitBranch:
    name = "near-limit"
    chat_model = SimpleNamespace(is_cli=False)
    token_budget = SimpleNamespace(
        is_critical=True,
        used=95_000,
        limit=100_000,
        usage_pct=0.95,
    )

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def operate(self, *, instruction: str) -> str:
        self.calls.append(instruction)
        return "prose without an emission"


@pytest.mark.asyncio
async def test_critical_context_skips_repair_that_can_only_overflow() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    branch = _NearLimitBranch()

    await run.operate_with_repair(
        branch,
        "initial instruction",
        arrived=lambda: False,
        retries=1,
    )

    assert branch.calls == ["initial instruction"]
    assert any(
        event["type"] == "emission_repair_skipped" and event["reason"] == "context_critical"
        for event in events
    )
    assert run._emission_failures == ["near-limit x1"]


class _PartiallyFailingReview(ReviewEngine):
    def __init__(self) -> None:
        super().__init__(
            dimensions=("broken", "healthy"),
            verify_clean=False,
        )
        self.healthy_started = asyncio.Event()
        self.healthy_finished = asyncio.Event()

    async def _review_dimension(self, run, artifact: str, dimension: str) -> None:
        if dimension == "broken":
            await self.healthy_started.wait()
            raise ProviderContextError("provider context overflow")
        self.healthy_started.set()
        await asyncio.sleep(0.05)
        self.healthy_finished.set()

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...]) -> str:
        assert self.healthy_finished.is_set()
        return "healthy dimension survived"


@pytest.mark.asyncio
async def test_review_dimension_failure_does_not_cancel_siblings_or_verdict() -> None:
    events: list[dict] = []
    engine = _PartiallyFailingReview()

    result = await engine.run("artifact", on_event=events.append)

    assert result == "healthy dimension survived"
    assert result.degraded is True
    assert result.skipped == ["review-broken (ProviderContextError)"]
    assert any(
        event["type"] == "dimension_failed"
        and event["dimension"] == "broken"
        and event["error_type"] == "ProviderContextError"
        for event in events
    )
