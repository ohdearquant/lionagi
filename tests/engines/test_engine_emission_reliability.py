# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace

import anyio
import pytest

from lionagi.engines.engine import Engine, EngineBudgetError
from lionagi.engines.review import ReviewEngine, _is_all_isolated_failure
from lionagi.ln.concurrency._compat import ExceptionGroup
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


class _TransportFailingReview(ReviewEngine):
    """One dimension dies of `failure`; the sibling must still finish and reach a verdict."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__(dimensions=("broken", "healthy"), verify_clean=False)
        self._failure = failure
        self.healthy_started = asyncio.Event()
        self.healthy_finished = asyncio.Event()

    async def _review_dimension(self, run, artifact: str, dimension: str) -> None:
        if dimension == "broken":
            await self.healthy_started.wait()
            raise self._failure
        self.healthy_started.set()
        await asyncio.sleep(0.05)
        self.healthy_finished.set()

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...]) -> str:
        assert self.healthy_finished.is_set()
        return "healthy dimension survived"


def _mcp_error(message: str) -> BaseException:
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    return McpError(ErrorData(code=-32000, message=message))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_failure", "expected_label"),
    [
        (lambda: anyio.ClosedResourceError(), "ClosedResourceError"),
        (lambda: anyio.BrokenResourceError(), "BrokenResourceError"),
        # The MCP SDK reads replies with `await response_stream_reader.receive()`,
        # which raises EndOfStream (not ClosedResourceError) once the peer closes.
        (lambda: anyio.EndOfStream(), "EndOfStream"),
        (
            lambda: ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [anyio.ClosedResourceError(), anyio.ClosedResourceError()],
            ),
            "ClosedResourceError",
        ),
        (
            pytest.param(
                lambda: _mcp_error("Connection closed"),
                "McpError",
                marks=pytest.mark.skipif(
                    importlib.util.find_spec("mcp") is None,
                    reason="mcp is an optional extra",
                ),
            )
        ),
    ],
)
async def test_review_isolates_transport_failures_like_provider_failures(
    make_failure, expected_label: str
) -> None:
    """A dropped transport is a per-dimension failure, so it must degrade one dimension, not kill the run.

    Before this, only ProviderError was isolated. A dropped MCP connection
    raises the MCP SDK's McpError (an Exception, not a ProviderError) and a
    dropped stream raises anyio's, so both escaped the isolation clause and
    propagated to the run-level handler that cancels every sibling — turning
    one dead dimension into a run that ends with no verdict at all.
    """
    events: list[dict] = []
    engine = _TransportFailingReview(make_failure())

    result = await engine.run("artifact", on_event=events.append)

    assert result == "healthy dimension survived"
    assert result.degraded is True
    assert result.skipped == [f"review-broken ({expected_label})"]
    assert any(
        event["type"] == "dimension_failed"
        and event["dimension"] == "broken"
        and event["error_type"] == expected_label
        for event in events
    )


def test_isolation_predicate_refuses_a_group_carrying_a_non_transport_leaf() -> None:
    """Isolate only when EVERY leaf is a transport/provider failure.

    A group mixing a transport drop with budget exhaustion must propagate:
    swallowing it would launder a run-wide stop into a per-dimension degrade
    and hide it behind a verdict that looks reasoned.
    """
    all_transport = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [anyio.ClosedResourceError(), anyio.BrokenResourceError()],
    )
    assert _is_all_isolated_failure(all_transport) is True

    mixed = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [anyio.ClosedResourceError(), EngineBudgetError("agent budget exhausted (12/12)")],
    )
    assert _is_all_isolated_failure(mixed) is False

    nested_mixed = ExceptionGroup(
        "outer",
        [ExceptionGroup("inner", [anyio.ClosedResourceError(), EngineBudgetError("exhausted")])],
    )
    assert _is_all_isolated_failure(nested_mixed) is False


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp is an optional extra")
def test_application_mcp_errors_are_not_isolated_as_transport_failures() -> None:
    """Only connection-shaped McpErrors are per-dimension transport failures.

    The SDK spells just two conditions as McpError itself: connection closed
    and request timeout. Every other McpError relays a server-side error —
    an authorization refusal, an application failure — which describes the
    request, not the wire. Swallowing those as transport drops turns e.g. a
    permission denial into a silent one-dimension degrade.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    connection_closed = McpError(ErrorData(code=-32000, message="Connection closed"))
    assert _is_all_isolated_failure(connection_closed) is True

    permission_denied = McpError(ErrorData(code=-32603, message="permission denied"))
    assert _is_all_isolated_failure(permission_denied) is False

    mixed = ExceptionGroup(
        "unhandled errors in a TaskGroup", [connection_closed, permission_denied]
    )
    assert _is_all_isolated_failure(mixed) is False


def test_a_broken_mcp_install_is_not_silently_treated_as_absent(monkeypatch) -> None:
    """An mcp that fails to import must raise, not disable isolation quietly.

    Reporting a broken install as "extra not present" would silently drop
    McpError out of the isolated set, and the first symptom would be a run
    dying with no verdict, far from the cause.
    """
    import builtins

    from lionagi.engines import review as review_mod

    review_mod._mcp_error_type.cache_clear()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp.shared.exceptions":
            # mcp itself is importable; one of ITS dependencies is missing.
            raise ModuleNotFoundError("No module named 'pydantic_core'", name="pydantic_core")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModuleNotFoundError):
        review_mod._mcp_error_type()
    review_mod._mcp_error_type.cache_clear()

    # The subtler arm: the top-level package resolved but the submodule itself
    # is missing. exc.name is then "mcp.shared.exceptions", which a prefix
    # check reads as "mcp is absent" — it is not, the install is broken.
    def fake_import_submodule(name, *args, **kwargs):
        if name == "mcp.shared.exceptions":
            raise ModuleNotFoundError(
                "No module named 'mcp.shared.exceptions'", name="mcp.shared.exceptions"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import_submodule)
    with pytest.raises(ModuleNotFoundError):
        review_mod._mcp_error_type()
    review_mod._mcp_error_type.cache_clear()


def test_a_missing_mcp_extra_is_a_normal_configuration(monkeypatch) -> None:
    """The other arm: mcp genuinely absent yields None rather than raising."""
    import builtins

    from lionagi.engines import review as review_mod

    review_mod._mcp_error_type.cache_clear()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp.shared.exceptions":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert review_mod._mcp_error_type() is None
    review_mod._mcp_error_type.cache_clear()
