# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A judge whose failure carries a cyclic cause chain still reads as a pass.

``Engine.judge`` walks ``__cause__`` to find a provider refusal that arrived
wrapped in the model layer's own error. A chain that loops back on itself makes
that walk never end, and the walk is synchronous, so the event loop freezes with
it. The walk must stop on a repeat, and the gate must still fail open.
"""

from __future__ import annotations

import contextlib
import signal
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lionagi import Branch, iModel
from lionagi.engines.engine import Engine

pytestmark = pytest.mark.asyncio

# A walk that does not end blocks the event loop, so only the alarm can stop it
# and turn the regression into a failure instead of a hung suite.
_SIGALRM_ONLY = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"), reason="needs SIGALRM to bound a walk that does not end"
)
_HANG_BOUND_S = 15


class _JudgeWhoseCallRaises:
    """Stands in for the judge agent; the call raises the error it was handed."""

    name = "judge"

    def __init__(self, error: BaseException):
        self._error = error

    async def operate(self, *, instruction):
        raise self._error


async def _judge(engine: Engine, agent) -> tuple[bool, list[tuple]]:
    run = engine.new_run()
    events: list[tuple] = []
    run.notify = lambda kind, **kw: events.append((kind, kw))

    async def make_agent(*args, **kwargs):
        return agent

    run.make_agent = make_agent
    return await engine.judge(run, "e1", "a subject"), events


def _cyclic_chain(*errors: BaseException) -> BaseException:
    """Link each error to the next, then the last back to the first."""
    for current, following in zip(errors, errors[1:]):
        current.__cause__ = following
    errors[-1].__cause__ = errors[0]
    return errors[0]


@pytest.mark.timeout(_HANG_BOUND_S, method="signal")
@_SIGALRM_ONLY
async def test_a_self_caused_error_fails_open_instead_of_walking_forever():
    error = RuntimeError("the provider refused the key")
    error.__cause__ = error

    allowed, events = await _judge(
        Engine(judge_model="openai/gpt-4.1"), _JudgeWhoseCallRaises(error)
    )

    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


@pytest.mark.timeout(_HANG_BOUND_S, method="signal")
@_SIGALRM_ONLY
async def test_a_two_element_cause_cycle_fails_open_instead_of_walking_forever():
    first = RuntimeError("the provider refused the key")
    second = ValueError("the layer above it")
    _cyclic_chain(first, second)

    allowed, events = await _judge(
        Engine(judge_model="openai/gpt-4.1"), _JudgeWhoseCallRaises(first)
    )

    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


@pytest.mark.timeout(_HANG_BOUND_S, method="signal")
@_SIGALRM_ONLY
async def test_a_refusal_behind_a_cyclic_chain_is_still_raised():
    """The stop must not turn a refusal into a pass when the chain also loops."""
    from lionagi._errors import ContainmentRefusedError

    refusal = ContainmentRefusedError("codex refuses its containment")
    wrapper = RuntimeError("the layer above it")
    _cyclic_chain(wrapper, refusal)

    with pytest.raises(ContainmentRefusedError, match="refuses its containment"):
        await _judge(Engine(judge_model="openai/gpt-4.1"), _JudgeWhoseCallRaises(wrapper))


@contextlib.asynccontextmanager
async def refusing_server():
    """A local server that answers every request with the provider's 4xx shape.

    Started inside the test rather than from an async fixture: an async fixture
    runs on its own loop, and a server bound to that loop is unreachable from the
    test's.
    """

    async def answer(request: web.Request):
        await request.read()
        return web.json_response(
            {"error": {"message": "the key is wrong", "type": "invalid_request_error"}},
            status=401,
        )

    server = TestServer(web.Application())
    server.app.router.add_route("POST", "/{tail:.*}", answer)
    await server.start_server()
    try:
        yield str(server.make_url("/")).rstrip("/")
    finally:
        await server.close()


def _local_model(base_url: str) -> iModel:
    return iModel(
        provider="openai",
        model="gpt-4.1",
        base_url=f"{base_url}/v1",
        api_key="not-a-key",
        allow_local_network=True,
    )


@pytest.mark.timeout(20, method="signal")
@_SIGALRM_ONLY
async def test_a_judge_on_a_provider_4xx_fails_open_with_an_acyclic_chain():
    """The real 4xx path over a local server: a 401 must not freeze the judge.

    Every layer above the transport is production code — the endpoint's 4xx
    sentinel, the retry layer's unwrap and the invocation wrapper — so this is
    the chain that froze the judge. The server is in-process on the loopback
    interface; no external network is used.
    """
    async with refusing_server() as base_url:
        agent = Branch(chat_model=_local_model(base_url))
        with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
            allowed, events = await _judge(Engine(judge_model="openai/gpt-4.1"), agent)

    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


@pytest.mark.timeout(20, method="signal")
@_SIGALRM_ONLY
async def test_the_chain_from_a_provider_4xx_ends_and_names_the_response():
    async with refusing_server() as base_url:
        branch = Branch(chat_model=_local_model(base_url))

        with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
            with pytest.raises(Exception) as info:
                await branch.operate(instruction="go")

    seen, cause, kinds = set(), info.value, []
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        kinds.append(type(cause).__name__)
        assert cause.__cause__ is not cause
        cause = cause.__cause__

    assert len(seen) <= 8
    assert "ClientResponseError" in kinds
