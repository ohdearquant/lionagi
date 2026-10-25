# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A judge whose failure carries a cyclic cause chain still reads as a pass.

``Engine.judge`` walks ``__cause__`` and ``__context__`` to find a provider
refusal that arrived wrapped in the model layer's own error. A chain that loops back on itself makes
that walk never end, and the walk is synchronous, so the event loop freezes with
it. The walk must stop on a repeat, and the gate must still fail open.
"""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from lionagi import Branch, iModel
from lionagi.service.connections.endpoint import Endpoint
from lionhub.orchestration.engines.engine import Engine

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


def _wrapped_without_from(refusal: BaseException, *, hide: bool) -> BaseException:
    """An error raised while handling *refusal*, with no ``from`` (or ``from None``)."""
    try:
        raise refusal
    except type(refusal):
        try:
            if hide:
                raise RuntimeError("the layer above it") from None
            raise RuntimeError("the layer above it")
        except RuntimeError as wrapper:
            return wrapper


@pytest.mark.parametrize("hide", [False, True], ids=["implicit", "from-none"])
async def test_a_refusal_left_as_the_context_of_a_wrapper_is_raised(hide):
    """A wrapper that raises without ``from`` keeps the refusal only as its context."""
    from lionagi._errors import ContainmentRefusedError

    refusal = ContainmentRefusedError("codex refuses its containment")
    wrapper = _wrapped_without_from(refusal, hide=hide)
    assert wrapper.__cause__ is None
    assert wrapper.__context__ is refusal

    with pytest.raises(ContainmentRefusedError, match="refuses its containment"):
        await _judge(Engine(judge_model="openai/gpt-4.1"), _JudgeWhoseCallRaises(wrapper))


@pytest.mark.timeout(_HANG_BOUND_S, method="signal")
@_SIGALRM_ONLY
async def test_a_two_element_context_cycle_fails_open_instead_of_walking_forever():
    first = RuntimeError("the provider refused the key")
    second = ValueError("the layer above it")
    first.__context__ = second
    second.__context__ = first

    allowed, events = await _judge(
        Engine(judge_model="openai/gpt-4.1"), _JudgeWhoseCallRaises(first)
    )

    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


async def refusing_server(monkeypatch):
    """A MockTransport returns the provider's 4xx shape without using a network."""

    def answer(request):
        return httpx.Response(
            401,
            json={"error": {"message": "the key is wrong", "type": "invalid_request_error"}},
            request=request,
        )

    def create_client(self, *args, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(answer))

    monkeypatch.setattr(Endpoint, "_create_http_session", create_client)
    monkeypatch.setattr(Endpoint, "_assert_ssrf_safe_url", lambda self: None)
    return "https://api.example.test"


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
async def test_a_judge_on_a_provider_4xx_fails_open_with_an_acyclic_chain(monkeypatch):
    """The real 4xx path through MockTransport: a 401 must not freeze the judge.

    Every layer above the transport is production code: the endpoint's 4xx
    sentinel, the retry layer's unwrap and the invocation wrapper.
    """
    base_url = await refusing_server(monkeypatch)
    agent = Branch(chat_model=_local_model(base_url))
    with patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock()):
        allowed, events = await _judge(Engine(judge_model="openai/gpt-4.1"), agent)

    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


@pytest.mark.timeout(20, method="signal")
@_SIGALRM_ONLY
async def test_the_chain_from_a_provider_4xx_ends_and_names_the_response(monkeypatch):
    base_url = await refusing_server(monkeypatch)
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
    assert "APIClientError" in kinds


async def test_an_uncontained_judge_raises_a_refusal_its_call_raises():
    """The refusal itself, with nothing wrapping it, is the first error the walk reads."""
    from lionagi._errors import ContainmentRefusedError

    refusal = ContainmentRefusedError("codex refuses its containment")

    with pytest.raises(ContainmentRefusedError, match="refuses its containment"):
        await _judge(Engine(judge_model="openai/gpt-4.1"), _JudgeWhoseCallRaises(refusal))


class _Holder:
    """A link that is not an exception, holding whatever it is handed."""

    def __init__(self, cause=None, context=None):
        self.__cause__ = cause
        self.__context__ = context


class _NoContext:
    __cause__ = None


def _odd_links(link) -> RuntimeError:
    class OddLinks(RuntimeError):
        @property
        def __cause__(self):
            return link

    return OddLinks("odd links")


async def _judge_outside_the_handler(error: BaseException):
    # Read outside the handler: an error raised with this chain as its context could not be
    # reported by a traceback that walks the same links.
    raised = None
    allowed, events = None, []
    try:
        allowed, events = await _judge(
            Engine(judge_model="openai/gpt-4.1"), _JudgeWhoseCallRaises(error)
        )
    except Exception as exc:
        raised = exc
    return raised, allowed, events


async def test_a_link_that_is_not_an_exception_and_holds_nothing_fails_open():
    """An error class may answer ``__cause__`` with an object that has no ``__context__``; the
    walk reads what it can and the judge fails open as for any other error."""
    raised, allowed, events = await _judge_outside_the_handler(_odd_links(_NoContext()))
    assert raised is None, repr(raised)
    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


@pytest.mark.parametrize("held_as", ["cause", "context", "two-links-down"])
async def test_a_refusal_behind_a_link_that_is_not_an_exception_is_raised(held_as):
    from lionagi._errors import ContainmentRefusedError

    refusal = ContainmentRefusedError("codex refuses its containment")
    if held_as == "cause":
        link = _Holder(cause=refusal)
    elif held_as == "context":
        link = _Holder(context=refusal)
    else:
        link = _Holder(cause=_Holder(cause=refusal))
    raised, _, _ = await _judge_outside_the_handler(_odd_links(link))
    assert raised is refusal


class _Minted(RuntimeError):
    """An error whose ``__cause__`` builds a new link on every read, ending in *last*."""

    def __init__(self, depth: int, last):
        super().__init__(f"link {depth}")
        self._depth, self._last = depth, last

    @property
    def __cause__(self):
        if self._depth == 0:
            return self._last
        return _Minted(self._depth - 1, self._last)


@pytest.mark.parametrize("depth", [1, 3, 6])
async def test_a_refusal_behind_links_built_on_each_read_is_raised(depth):
    """Each link is kept until the walk ends, so a link built on a later read cannot take the
    address of one already read and be skipped as seen."""
    from lionagi._errors import ContainmentRefusedError

    refusal = ContainmentRefusedError("codex refuses its containment")
    raised, _, _ = await _judge_outside_the_handler(_Minted(depth, refusal))
    assert raised is refusal


class _Endless(RuntimeError):
    """Builds a new link on every read of ``__cause__``, for ten times the walk's limit, so a walk
    without a limit ends here too rather than exhausting memory."""

    reads = 0

    @property
    def __cause__(self):
        type(self).reads += 1
        if type(self).reads > 1_000:
            return None
        return _Endless("again")


async def test_a_chain_that_builds_a_new_link_on_every_read_ends_at_the_limit_and_fails_open():
    _Endless.reads = 0
    raised, allowed, events = await _judge_outside_the_handler(_Endless("first"))
    assert _Endless.reads == 100
    assert raised is None, repr(raised)
    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


async def test_an_object_that_only_claims_to_be_a_refusal_is_not_raised():
    from lionagi._errors import ContainmentRefusedError

    class Claims:
        __cause__ = None
        __context__ = None

        @property
        def __class__(self):
            return ContainmentRefusedError

    claim = Claims()
    assert isinstance(claim, ContainmentRefusedError)
    raised, allowed, events = await _judge_outside_the_handler(_odd_links(claim))
    assert raised is None, repr(raised)
    assert allowed is True
    assert [kind for kind, _ in events] == ["judge_error"]


async def test_a_link_that_cannot_be_read_ends_that_branch_only():
    from lionagi._errors import ContainmentRefusedError

    class UnreadableCause(RuntimeError):
        @property
        def __cause__(self):
            raise ValueError("this link cannot be read")

    refusal = ContainmentRefusedError("codex refuses its containment")
    error = UnreadableCause("unreadable")
    error.__context__ = refusal
    raised, _, _ = await _judge_outside_the_handler(error)
    assert raised is refusal


async def test_a_context_that_cannot_be_read_still_lets_the_cause_be_read():
    """The context is read before the cause; a read that fails ends only the link it reads."""
    from lionagi._errors import ContainmentRefusedError

    class UnreadableContext(RuntimeError):
        @property
        def __context__(self):
            raise ValueError("this link cannot be read")

    refusal = ContainmentRefusedError("codex refuses its containment")
    error = UnreadableContext("unreadable")
    error.__cause__ = refusal
    raised, _, _ = await _judge_outside_the_handler(error)
    assert raised is refusal


class _Interrupt(BaseException):
    """Stands in for an interrupt or a cancellation, which no read may swallow."""


async def test_an_interrupt_raised_by_a_read_leaves_the_judge():
    class InterruptingCause(RuntimeError):
        @property
        def __cause__(self):
            raise _Interrupt("stop")

    with pytest.raises(_Interrupt, match="stop"):
        await _judge(
            Engine(judge_model="openai/gpt-4.1"),
            _JudgeWhoseCallRaises(InterruptingCause("interrupted")),
        )


def _linear_chain(length: int, last: BaseException) -> RuntimeError:
    """*length* links, each the cause of the one before it, the last of them *last*."""
    links: list[BaseException] = [RuntimeError(f"link {i}") for i in range(1, length)]
    links.append(last)
    for current, following in zip(links, links[1:]):
        current.__cause__ = following
    return links[0]


@pytest.mark.parametrize(("position", "raised"), [(100, True), (101, False)])
async def test_a_refusal_is_raised_within_the_first_hundred_links_and_not_past_them(
    position, raised
):
    from lionagi._errors import ContainmentRefusedError

    refusal = ContainmentRefusedError("codex refuses its containment")
    error, allowed, events = await _judge_outside_the_handler(_linear_chain(position, refusal))
    if raised:
        assert error is refusal
    else:
        assert error is None, repr(error)
        assert allowed is True
        assert [kind for kind, _ in events] == ["judge_error"]


async def test_a_refusal_one_link_away_is_raised_beside_a_branch_longer_than_the_limit():
    """The links nearest the error are read first, so a long chain of causes cannot spend the
    limit before the refusal held as the error's context is read."""
    from lionagi._errors import ContainmentRefusedError

    refusal = ContainmentRefusedError("codex refuses its containment")
    error = RuntimeError("the judge failed")
    error.__context__ = refusal
    error.__cause__ = _linear_chain(150, RuntimeError("the end of a long branch"))
    raised, _, _ = await _judge_outside_the_handler(error)
    assert raised is refusal


async def test_a_containment_refusal_naming_an_unreadable_override_is_raised_by_the_judge(
    tmp_path,
):
    """A contained request refuses an override key containment cannot read with the type a
    containment refusal carries, so a judge that catches that type refuses the request instead
    of reading its failure as a pass."""
    from lionagi._errors import ContainmentRefusedError
    from lionagi.providers.openai.codex import CodexCodeRequest

    request = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=True
    ).model_copy(update={"config_overrides": {"a b": 1}})
    with pytest.raises(ContainmentRefusedError) as refused:
        request.as_cmd_args()

    raised, allowed, events = await _judge_outside_the_handler(refused.value)

    assert raised is not None, "the judge read the containment refusal as a pass"
    assert isinstance(raised, ContainmentRefusedError)
    assert "is not a TOML key" in str(raised)
    assert allowed is None
    assert events == []


async def test_a_containment_refusal_naming_an_unreadable_override_keeps_the_type_at_spawn(
    tmp_path, monkeypatch
):
    """The same refusal, raised where the process would start, still carries the type the
    judge reads as a refusal rather than as a pass. No process is started."""
    from lionagi._errors import ContainmentRefusedError
    from lionagi.providers import _cli_subprocess
    from lionagi.providers.openai import codex as codex_mod
    from lionagi.providers.openai.codex import CodexCodeRequest

    def spawned(*_args, **_kwargs):
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(_cli_subprocess.asyncio, "create_subprocess_exec", spawned)
    request = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=True
    ).model_copy(update={"config_overrides": {"a b": 1}})

    with pytest.raises(ContainmentRefusedError) as refused:
        async for _ in codex_mod._ndjson_from_cli(request):
            pass

    raised, allowed, events = await _judge_outside_the_handler(refused.value)

    assert isinstance(raised, ContainmentRefusedError)
    assert "is not a TOML key" in str(raised)
    assert allowed is None
    assert events == []


async def test_a_construction_refusal_naming_an_unreadable_override_is_raised_by_the_judge(
    tmp_path,
):
    """A key containment cannot read refuses the request at construction. That refusal is a
    CLIInputRefusedError, so pydantic does not wrap it, and it carries a containment refusal
    as its cause, so a judge whose engine is built over such a key raises instead of passing."""
    from lionagi._errors import CLIInputRefusedError, ContainmentRefusedError
    from lionagi.providers.openai.codex import CodexCodeRequest

    with pytest.raises(CLIInputRefusedError) as refused:
        CodexCodeRequest(
            prompt="hi",
            repo=tmp_path,
            contain_account_integrations=True,
            config_overrides={"a b": 1},
        )

    assert type(refused.value.__cause__) is ContainmentRefusedError
    assert str(refused.value.__cause__) == str(refused.value)
    assert "a b" not in str(refused.value)

    raised, allowed, events = await _judge_outside_the_handler(refused.value)

    assert isinstance(raised, ContainmentRefusedError)
    assert "is not a TOML key" in str(raised)
    assert allowed is None
    assert events == []
