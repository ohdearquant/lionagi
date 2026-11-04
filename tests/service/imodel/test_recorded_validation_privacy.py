# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A validation error recorded on an API event cannot expose its input."""

import pytest
from pydantic import ValidationError

from lionagi._errors import ExecutionError
from lionagi.operations.chat.chat import chat
from lionagi.operations.run.run import RunParam, run
from lionagi.operations.types import ChatParam
from lionagi.protocols.generic import EventStatus
from lionagi.protocols.messages import AssistantResponse
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch

_MARKER = "sensitive fixture content"


def test_no_backport_probe_uses_the_local_compatibility_shim():
    import os

    from lionagi.ln.concurrency._compat import ExceptionGroup

    if os.environ.get("LIONAGI_TEST_NO_EG") == "1":
        assert ExceptionGroup.__module__ == "lionagi.ln.concurrency._compat"


def test_no_backport_shim_can_represent_a_cyclic_group():
    import os

    from lionagi.ln.concurrency._compat import ExceptionGroup

    if os.environ.get("LIONAGI_TEST_NO_EG") == "1":
        group = ExceptionGroup("cycle", [RuntimeError("ordinary")])
        group.exceptions = (group,)
        assert group.exceptions[0] is group


def _private_error():
    return ValidationError.from_exception_data(
        "ProviderRequest", [{"type": "int_parsing", "loc": (), "input": _MARKER}]
    )


def _assert_safe_error_graph(error):
    from lionagi.ln.concurrency._compat import BaseExceptionGroup

    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if (
            isinstance(current, ValidationError)
            or _MARKER in str(current)
            or _MARKER in repr(current)
        ):
            pytest.fail("exception graph retained validation input", pytrace=False)
        pending.extend((current.__cause__, current.__context__))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_group_member_validation_chain_is_not_reachable(link, mixed):
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        ordinary = RuntimeError("ordinary failure")
        setattr(ordinary, link, _private_error())
        members = [ordinary]
        if mixed:
            members.append(_private_error())
        call.execution.error = ExceptionGroup("outer", members)
        assert call.execution.error.exceptions[0] is ordinary
        assert ordinary.args == ("ordinary failure",)
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_group_member_cyclic_validation_chain_is_not_reachable():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        first, second = RuntimeError("first"), RuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first
        second.__context__ = _private_error()
        call.execution.error = ExceptionGroup("cycle", [first])
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
async def test_member_chain_to_nested_group_does_not_retain_validation(link):
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        member = RuntimeError("ordinary")
        other = LookupError("other")
        nested = ExceptionGroup("inner", [other, _private_error()])
        group = ExceptionGroup("wrapper", [nested])
        cause = OSError("group cause")
        context = RuntimeError("group context")
        group.__cause__ = cause
        group.__context__ = context
        group.__suppress_context__ = False
        if hasattr(group, "add_note"):
            group.add_note("safe note")
        try:
            raise group
        except ExceptionGroup as raised:
            original_traceback = raised.__traceback__
        setattr(member, link, group)
        call.execution.error = ExceptionGroup("outer", [member])
        assert call.execution.error.exceptions[0] is member
        cleaned = getattr(member, link)
        assert cleaned is not group and cleaned.message == "wrapper"
        assert cleaned.exceptions[0].message == "inner"
        assert cleaned.exceptions[0].exceptions == (other,)
        assert cleaned.__cause__ is cause and cleaned.__context__ is context
        assert cleaned.__suppress_context__ is False
        assert cleaned.__traceback__ is original_traceback
        if hasattr(group, "__notes__"):
            assert cleaned.__notes__ == ["safe note"]
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
async def test_member_chain_to_all_private_group_is_cut(link):
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        member = RuntimeError("ordinary")
        group = ExceptionGroup("outer", [ExceptionGroup("inner", [_private_error()])])
        setattr(member, link, group)
        call.execution.error = ExceptionGroup("event", [member])
        assert call.execution.error.exceptions[0] is member
        assert getattr(member, link) is None
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_group_via_cause_with_cycle_is_sanitized_once():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        member = RuntimeError("ordinary")
        group = ExceptionGroup("cause", [member, _private_error()])
        member.__cause__ = group
        call.execution.error = ExceptionGroup("event", [member])
        assert member.__cause__ is not group
        assert member.__cause__.exceptions == (member,)
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["self", "raise", "different", "well_behaved"])
async def test_linked_group_derive_cannot_retain_or_substitute_members(mode):
    from lionagi.ln.concurrency._compat import ExceptionGroup

    class CustomGroup(ExceptionGroup):
        def derive(self, children):
            if mode == "self":
                return self
            if mode == "raise":
                raise ValueError("bad derive")
            if mode == "different":
                return ExceptionGroup(self.message, [RuntimeError("substituted")])
            return CustomGroup(self.message, children)

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        other = LookupError("ordinary")
        group = CustomGroup("nested", [other, _private_error()])
        cause = RuntimeError("safe cause")
        group.__cause__ = cause
        member = RuntimeError("outer")
        member.__context__ = group
        call.execution.error = ExceptionGroup("event", [member])
        cleaned = member.__context__
        assert isinstance(cleaned, ExceptionGroup)
        assert cleaned.exceptions == (other,) and cleaned.exceptions[0] is other
        assert cleaned.__cause__ is cause
        if mode == "well_behaved":
            assert type(cleaned) is CustomGroup
        else:
            assert type(cleaned) is ExceptionGroup
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_linked_group_derive_cancellation_propagates():
    import asyncio

    from lionagi.ln.concurrency._compat import ExceptionGroup

    class CancelledGroup(ExceptionGroup):
        def derive(self, children):
            raise asyncio.CancelledError()

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        member = RuntimeError("outer")
        member.__cause__ = CancelledGroup("nested", [LookupError("ordinary"), _private_error()])
        with pytest.raises(asyncio.CancelledError):
            call.execution.error = ExceptionGroup("event", [member])
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_deep_group_via_cause_is_sanitized_without_recursion():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        other = LookupError("ordinary")
        group = ExceptionGroup("leaf", [other, _private_error()])
        for _ in range(1200):
            group = ExceptionGroup("nested", [group])
        member = RuntimeError("ordinary")
        member.__cause__ = group
        call.execution.error = ExceptionGroup("event", [member])
        cleaned = member.__cause__
        for _ in range(1200):
            cleaned = cleaned.exceptions[0]
        assert cleaned.exceptions == (other,)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_deep_member_cause_chain_is_sanitized_without_recursion():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        current = _private_error()
        for _ in range(1500):
            parent = RuntimeError("ordinary")
            parent.__cause__ = current
            current = parent
        call.execution.error = ExceptionGroup("deep", [current])
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_group_member_custom_setattr_cannot_keep_validation_cause():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    class CustomError(RuntimeError):
        def __setattr__(self, name, value):
            if name in {"__cause__", "__context__"}:
                raise RuntimeError("custom exception setter")
            super().__setattr__(name, value)

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        ordinary = CustomError("ordinary failure")
        BaseException.__cause__.__set__(ordinary, _private_error())
        call.execution.error = ExceptionGroup("outer", [ordinary])
        assert call.execution.error.exceptions[0] is ordinary
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["create", "invoke"])
async def test_request_options_refusal_has_no_validation_graph(entry):
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    caught = None
    try:
        try:
            if entry == "create":
                model.create_api_calling(
                    messages=[{"role": "user", "content": "hello"}], temperature=_MARKER
                )
            else:
                await model.invoke(
                    messages=[{"role": "user", "content": "hello"}], temperature=_MARKER
                )
        except ValueError as exc:
            caught = exc
        assert caught is not None
        assert "1 validation error" in str(caught)
        _assert_safe_error_graph(caught)
        traceback = caught.__traceback__
        while traceback is not None:
            if any(
                isinstance(value, ValidationError) for value in traceback.tb_frame.f_locals.values()
            ):
                pytest.fail("exception traceback retained a validation error", pytrace=False)
            traceback = traceback.tb_next
    finally:
        await model.close()


async def _failed_call(monkeypatch, mode):
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    original = _private_error()

    async def fail_invoke(_self):
        raise original

    async def fail_stream(_self):
        raise original
        yield  # pragma: no cover

    monkeypatch.setattr(type(call), "_core_invoke", fail_invoke)
    monkeypatch.setattr(type(call), "_core_stream", fail_stream)
    caught = None
    try:
        if mode == "invoke":
            result = await model.invoke(api_call=call)
            assert result is call
        else:
            async for _ in model.stream(api_call=call):
                pass
    except Exception as error:
        caught = error
    finally:
        await model.close()
    assert call.status == EventStatus.FAILED
    if mode == "stream":
        assert isinstance(caught, ValueError)
    return call, original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_failed_event_contains_only_sanitized_validation_error(monkeypatch, mode):
    call, original = await _failed_call(monkeypatch, mode)
    error = call.execution.error
    if error is original or isinstance(error, ValidationError) or not isinstance(error, ValueError):
        pytest.fail("API event retained its original validation error", pytrace=False)
    if error.__cause__ is not None or error.__context__ is not None:
        pytest.fail("API event retained a validation exception chain", pytrace=False)
    if _MARKER in str(error) or _MARKER in repr(error):
        pytest.fail("API event retained validation input", pytrace=False)
    assert "1 validation error" in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_branch_log_dump_omits_recorded_validation_input(monkeypatch, tmp_path, mode):
    call, _ = await _failed_call(monkeypatch, mode)
    branch = Branch()
    await branch.emit_and_log(call)
    output = tmp_path / "events.json"
    branch.dump_logs(clear=False, persist_path=output)
    text = output.read_text()
    output.unlink()
    if _MARKER in text:
        pytest.fail("branch log dump retained validation input", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("outlet", ["message-and-chain", "branch-log"])
async def test_chat_failed_invoke_does_not_expose_recorded_validation(
    monkeypatch, tmp_path, outlet
):
    original = _private_error()

    async def fail_invoke(_self):
        raise original

    monkeypatch.setattr(APICalling, "_core_invoke", fail_invoke)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    branch = Branch(chat_model=model)
    caught = None
    try:
        await chat(branch, "hello", ChatParam.from_branch(branch))
    except Exception as error:
        caught = error
    finally:
        await model.close()
    assert isinstance(caught, ExecutionError)
    if outlet == "branch-log":
        output = tmp_path / "chat-events.json"
        branch.dump_logs(clear=False, persist_path=output)
        text = output.read_text()
        output.unlink()
        if _MARKER in text:
            pytest.fail("chat branch log retained validation input", pytrace=False)
    else:
        pending, seen = [caught], set()
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if (
                isinstance(current, ValidationError)
                or _MARKER in str(current)
                or _MARKER in repr(current)
            ):
                pytest.fail("chat error chain retained validation input", pytrace=False)
            pending.extend((current.__cause__, current.__context__))


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["normal", "benign"])
async def test_run_stream_metadata_omits_event_validation_input(monkeypatch, ending):
    original = _private_error()

    async def text_with_recorded_error(self):
        self.execution.error = original
        yield StreamChunk(type="text", content="hello")
        if ending == "benign":
            yield StreamChunk(type="error", metadata={"benign_eos": True})

    monkeypatch.setattr(APICalling, "_core_stream", text_with_recorded_error)
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    branch = Branch(chat_model=model)
    try:
        results = [item async for item in run(branch, "hello", RunParam())]
    finally:
        await model.close()
    response = next(item for item in results if isinstance(item, AssistantResponse))
    if _MARKER in repr(response.metadata["api_call_meta"]):
        pytest.fail("run metadata retained validation input", pytrace=False)


@pytest.mark.asyncio
async def test_stream_event_is_sanitized_before_the_first_chunk_reaches_caller(monkeypatch):
    original = _private_error()

    async def text_with_recorded_error(self):
        self.execution.error = original
        yield StreamChunk(type="text", content="hello")

    monkeypatch.setattr(APICalling, "_core_stream", text_with_recorded_error)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    seen = False
    try:
        async for _ in model.stream(api_call=call):
            seen = True
            if isinstance(call.execution.error, ValidationError):
                pytest.fail("stream caller saw original validation error", pytrace=False)
    finally:
        await model.close()
    assert seen


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["active", "other"])
async def test_stream_hook_cannot_yield_an_event_with_raw_validation(monkeypatch, which):
    original = _private_error()

    async def text_with_recorded_error(self):
        if which == "active":
            self.execution.error = original
        yield StreamChunk(type="text", content="hello")

    monkeypatch.setattr(APICalling, "_core_stream", text_with_recorded_error)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    other = await model.create_event(messages=[{"role": "user", "content": "other"}])
    if which == "other":
        other.execution.error = original
    emitted = call if which == "active" else other

    async def return_event(_chunk):
        return emitted

    monkeypatch.setattr(model, "process_chunk", return_event)
    seen = False
    try:
        async for item in model.stream(api_call=call):
            seen = True
            if item is not emitted or isinstance(item.execution.error, ValidationError):
                pytest.fail("stream emitted an event with raw validation", pytrace=False)
    finally:
        await model.close()
    assert seen


@pytest.mark.asyncio
async def test_stream_hook_receives_sanitized_provider_event(monkeypatch):
    original = _private_error()
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    other = await model.create_event(messages=[{"role": "user", "content": "other"}])
    other.execution.error = original

    async def event_chunk(_self):
        yield other

    async def inspect_event(chunk):
        if isinstance(chunk.execution.error, ValidationError):
            pytest.fail("stream hook saw an event with raw validation", pytrace=False)
        return chunk

    monkeypatch.setattr(APICalling, "_core_stream", event_chunk)
    monkeypatch.setattr(model, "process_chunk", inspect_event)
    try:
        results = [item async for item in model.stream(api_call=call)]
    finally:
        await model.close()
    assert results == [other]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_nonvalidation_recorded_error_keeps_its_identity(monkeypatch, mode):
    original = RuntimeError("ordinary failure")

    async def fail_invoke(_self):
        raise original

    async def fail_stream(_self):
        raise original
        yield  # pragma: no cover

    monkeypatch.setattr(APICalling, "_core_invoke", fail_invoke)
    monkeypatch.setattr(APICalling, "_core_stream", fail_stream)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    try:
        if mode == "invoke":
            await model.invoke(api_call=call)
        else:
            try:
                async for _ in model.stream(api_call=call):
                    pass
            except ValueError:
                pass
    finally:
        await model.close()
    assert call.execution.error is original


@pytest.mark.asyncio
async def test_stream_close_does_not_allow_late_validation_writes(monkeypatch):
    import asyncio

    original = _private_error()
    teardown = asyncio.Event()

    async def text_then_write_on_close(self):
        try:
            yield StreamChunk(type="text", content="hello")
        finally:
            self.execution.error = original
            teardown.set()

    monkeypatch.setattr(APICalling, "_core_stream", text_then_write_on_close)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    stream = model.stream(api_call=call)
    try:
        await stream.__anext__()
        await stream.aclose()
        try:
            await asyncio.wait_for(teardown.wait(), timeout=1)
        except asyncio.TimeoutError:
            pytest.fail("underlying stream teardown did not finish", pytrace=False)
        if isinstance(call.execution.error, ValidationError):
            pytest.fail("underlying stream close retained validation input", pytrace=False)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_deferred_invoke_does_not_write_validation_after_return(monkeypatch):
    import asyncio

    original = _private_error()

    async def fail_invoke(_self):
        raise original

    monkeypatch.setattr(APICalling, "_core_invoke", fail_invoke)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    await model.executor.start()
    ready = False

    async def permission(**_kwargs):
        return ready

    async def fast_timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(model.executor.processor, "request_permission", permission)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "wait_for", fast_timeout)
            returned = await model.invoke(api_call=call)
        assert returned is call and call.status == EventStatus.PENDING
        ready = True
        await model.executor.processor.process()
        assert call.status == EventStatus.FAILED
        if isinstance(call.execution.error, ValidationError):
            pytest.fail("deferred processing wrote raw validation after return", pytrace=False)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_recorded_exception_group_does_not_expose_validation_input(monkeypatch):
    original = _private_error()

    async def fail_invoke(_self):
        raise original

    monkeypatch.setattr(APICalling, "_core_invoke", fail_invoke)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    call.execution.error = RuntimeError("ordinary failure")
    try:
        await model.invoke(api_call=call)
    finally:
        await model.close()
    if _MARKER in repr(call.to_dict()):
        pytest.fail("recorded exception group retained validation input", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", ["direct", "create-event", "custom-event"])
async def test_imodel_created_event_sanitizes_direct_error_assignment(factory):
    class CustomCalling(APICalling):
        pass

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    if factory == "direct":
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    elif factory == "create-event":
        call = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    else:
        call = await model.create_event(
            create_event_type=CustomCalling, endpoint=model.endpoint, payload={}
        )
    original = _private_error()
    call.execution.error = original
    stored = call.execution.error
    if (
        isinstance(stored, ValidationError)
        or stored is original
        or not isinstance(stored, ValueError)
    ):
        pytest.fail("event assignment retained validation input", pytrace=False)
    if stored.__cause__ is not None or stored.__context__ is not None:
        pytest.fail("event assignment retained original exception chain", pytrace=False)
    assert "1 validation error" in str(stored)


@pytest.mark.asyncio
async def test_nested_error_assignment_preserves_other_group_members():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    first = RuntimeError("first")
    last = LookupError("last")
    inner = ExceptionGroup("inner", [_private_error(), last])
    call.execution.error = ExceptionGroup("outer", [first, inner])
    stored = call.execution.error
    if not isinstance(stored, ExceptionGroup) or stored.message != "outer":
        pytest.fail("outer exception group shape changed", pytrace=False)
    nested = stored.exceptions[1]
    if not isinstance(nested, ExceptionGroup) or nested.message != "inner":
        pytest.fail("nested exception group shape changed", pytrace=False)
    if stored.exceptions[0] is not first or nested.exceptions[1] is not last:
        pytest.fail("ordinary exception members changed", pytrace=False)
    leaf = nested.exceptions[0]
    if isinstance(leaf, ValidationError) or not isinstance(leaf, ValueError):
        pytest.fail("nested group retained validation input", pytrace=False)
    if leaf.__cause__ is not None or leaf.__context__ is not None or _MARKER in repr(stored):
        pytest.fail("nested group retained validation input or chain", pytrace=False)


@pytest.mark.asyncio
async def test_rebuilt_recorded_groups_preserve_safe_exception_metadata():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        ordinary = LookupError("ordinary")
        nested = ExceptionGroup("nested", [ordinary, _private_error()])
        group = ExceptionGroup("outer", [nested])
        safe_cause = OSError("safe cause")
        safe_context = RuntimeError("safe context")
        nested.__cause__ = safe_cause
        nested.__context__ = _private_error()
        group.__cause__ = safe_cause
        group.__context__ = safe_context
        nested.__suppress_context__ = False
        group.__suppress_context__ = False
        if hasattr(group, "add_note"):
            group.add_note("outer note")
            nested.add_note("nested note")
        try:
            raise group
        except ExceptionGroup as raised:
            traceback = raised.__traceback__
        call.execution.error = group
        stored = call.execution.error
        inner = stored.exceptions[0]
        assert stored is not group and inner is not nested
        assert inner.exceptions[0] is ordinary
        assert stored.__cause__ is safe_cause and stored.__context__ is safe_context
        assert inner.__cause__ is safe_cause and inner.__context__ is None
        assert stored.__suppress_context__ is False and inner.__suppress_context__ is False
        assert stored.__traceback__ is traceback
        if hasattr(group, "__notes__"):
            assert stored.__notes__ == ["outer note"] and inner.__notes__ == ["nested note"]
        _assert_safe_error_graph(stored)
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["root", "member"])
@pytest.mark.parametrize("mode", ["self", "raise", "different", "well_behaved"])
async def test_recorded_group_uses_checked_derive_and_preserves_metadata(level, mode):
    from lionagi.ln.concurrency._compat import ExceptionGroup

    calls = []

    class CustomGroup(ExceptionGroup):
        def derive(self, children):
            calls.append(1)
            if mode == "self":
                return self
            if mode == "raise":
                raise ValueError("bad derive")
            if mode == "different":
                return ExceptionGroup(self.message, [RuntimeError("substituted")])
            return CustomGroup(self.message, children)

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        other = LookupError("ordinary")
        group = CustomGroup("nested", [other, _private_error()])
        cause, context = OSError("safe cause"), RuntimeError("safe context")
        group.__cause__ = cause
        group.__context__ = context
        group.__suppress_context__ = False
        if hasattr(group, "add_note"):
            group.add_note("safe note")
        try:
            raise group
        except CustomGroup as raised:
            traceback = raised.__traceback__
        call.execution.error = group if level == "root" else ExceptionGroup("event", [group])
        cleaned = call.execution.error if level == "root" else call.execution.error.exceptions[0]
        assert calls and cleaned is not group
        assert cleaned.exceptions[0] is other and isinstance(cleaned.exceptions[1], ValueError)
        assert cleaned.__cause__ is cause and cleaned.__context__ is context
        assert cleaned.__suppress_context__ is False and cleaned.__traceback__ is traceback
        if hasattr(group, "__notes__"):
            assert cleaned.__notes__ == ["safe note"]
        assert type(cleaned) is (CustomGroup if mode == "well_behaved" else ExceptionGroup)
        _assert_safe_error_graph(call.execution.error)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_deep_exception_group_is_sanitized_without_recursing():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    nested = _private_error()
    for _ in range(1200):
        nested = ExceptionGroup("nested", [nested])
    call.execution.error = nested
    stored = call.execution.error
    for _ in range(1200):
        stored = stored.exceptions[0]
    if isinstance(stored, ValidationError) or not isinstance(stored, ValueError):
        pytest.fail("deep group retained validation input", pytrace=False)


@pytest.mark.asyncio
async def test_no_backport_cyclic_group_uses_a_fixed_placeholder():
    import os
    import sys

    from lionagi.ln.concurrency._compat import ExceptionGroup

    if os.environ.get("LIONAGI_TEST_NO_EG") != "1":
        pytest.skip("requires the no-backport compatibility probe")
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    other = RuntimeError("ordinary failure")
    cyclic = ExceptionGroup("cycle", [other])
    cyclic.exceptions = (cyclic, _private_error(), other)
    previous_trace = sys.gettrace()
    steps = 0

    def bound_probe(frame, event, _arg):
        nonlocal steps
        if frame.f_code.co_name == "_rebuild_validation_group" and event == "line":
            steps += 1
            if steps > 1000:
                raise RuntimeError("cyclic sanitizer exceeded the test bound")
        return bound_probe

    try:
        sys.settrace(bound_probe)
        call.execution.error = cyclic
    except RuntimeError:
        pytest.fail("cyclic group did not finish sanitization", pytrace=False)
    finally:
        sys.settrace(previous_trace)
    stored = call.execution.error
    if type(stored.exceptions[0]) is not ValueError:
        pytest.fail("cyclic branch did not become a safe placeholder", pytrace=False)
    if str(stored.exceptions[0]) != "API call failed: cyclic exception group":
        pytest.fail("cyclic branch did not use the fixed placeholder", pytrace=False)
    if stored.exceptions[2] is not other:
        pytest.fail("ordinary group member changed", pytrace=False)
    if isinstance(stored.exceptions[1], ValidationError):
        pytest.fail("cyclic group retained validation input", pytrace=False)


@pytest.mark.asyncio
async def test_no_backport_cycle_without_validation_keeps_its_identity():
    import os
    import sys

    from lionagi.ln.concurrency._compat import ExceptionGroup

    if os.environ.get("LIONAGI_TEST_NO_EG") != "1":
        pytest.skip("requires the no-backport compatibility probe")
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    ordinary = RuntimeError("ordinary failure")
    cyclic = ExceptionGroup("cycle", [ordinary])
    cyclic.exceptions = (cyclic, ordinary)
    previous_trace = sys.gettrace()
    steps = 0

    def bound_probe(frame, event, _arg):
        nonlocal steps
        if frame.f_code.co_name == "_rebuild_validation_group" and event == "line":
            steps += 1
            if steps > 1000:
                raise RuntimeError("cyclic sanitizer exceeded the test bound")
        return bound_probe

    try:
        sys.settrace(bound_probe)
        call.execution.error = cyclic
    except RuntimeError:
        pytest.fail("ordinary cyclic group did not finish", pytrace=False)
    finally:
        sys.settrace(previous_trace)
    if call.execution.error is not cyclic:
        pytest.fail("ordinary cyclic group changed identity", pytrace=False)


@pytest.mark.asyncio
async def test_unrelated_error_and_group_keep_their_identity():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    ordinary = RuntimeError("ordinary failure")
    call.execution.error = ordinary
    assert call.execution.error is ordinary
    group = ExceptionGroup("ordinary group", [ordinary])
    call.execution.error = group
    assert call.execution.error is group


@pytest.mark.asyncio
async def test_nonvalidation_group_member_loses_private_validation_cause():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    ordinary = RuntimeError("ordinary failure")
    caller_cause = _private_error()
    ordinary.__cause__ = caller_cause
    call.execution.error = ExceptionGroup("mixed", [ordinary, _private_error()])
    stored = call.execution.error
    assert stored.exceptions[0] is ordinary
    assert ordinary.__cause__ is None
    assert not isinstance(stored.exceptions[1], ValidationError)
    _assert_safe_error_graph(stored)


@pytest.mark.asyncio
async def test_nonvalidation_group_member_keeps_ordinary_cause():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    ordinary = RuntimeError("ordinary failure")
    ordinary_cause = LookupError("ordinary cause")
    ordinary.__cause__ = ordinary_cause
    call.execution.error = ExceptionGroup("mixed", [ordinary, _private_error()])
    assert call.execution.error.exceptions[0] is ordinary
    assert ordinary.__cause__ is ordinary_cause
    await model.close()


@pytest.mark.asyncio
async def test_member_preserves_text_and_nonvalidation_context_when_private_cause_is_cut():
    from lionagi.ln.concurrency._compat import ExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    try:
        call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
        ordinary = RuntimeError(_MARKER)
        context = LookupError("ordinary context")
        ordinary.__cause__ = _private_error()
        ordinary.__context__ = context
        ordinary.__suppress_context__ = False
        call.execution.error = ExceptionGroup("mixed", [ordinary])
        assert call.execution.error.exceptions[0] is ordinary
        assert ordinary.args == (_MARKER,)
        assert ordinary.__cause__ is None
        assert ordinary.__context__ is context
        assert ordinary.__suppress_context__ is False
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_base_exception_group_keeps_cancellation_member():
    import asyncio

    from lionagi.ln.concurrency._compat import BaseExceptionGroup

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    cancelled = asyncio.CancelledError()
    call.execution.error = BaseExceptionGroup("mixed", [_private_error(), cancelled])
    stored = call.execution.error
    if stored.exceptions[1] is not cancelled or isinstance(stored.exceptions[0], ValidationError):
        pytest.fail(
            "mixed group did not preserve cancellation and sanitize validation", pytrace=False
        )


@pytest.mark.asyncio
async def test_add_error_without_group_support_does_not_stringify_raw_validation(monkeypatch):
    import lionagi.protocols.generic.event as event_module

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    call.execution.error = RuntimeError("ordinary failure")
    monkeypatch.setattr(event_module, "ExceptionGroup", None)
    call.execution.add_error(_private_error())
    if _MARKER in str(call.execution.error) or _MARKER in repr(call.to_dict()):
        pytest.fail("fallback stringified raw validation input", pytrace=False)
    assert "ordinary failure" in str(call.execution.error)


@pytest.mark.asyncio
async def test_function_calling_keeps_validation_details():
    from lionagi.protocols.action.function_calling import FunctionCalling, Tool

    original = _private_error()

    def fail_tool():
        raise original

    event = FunctionCalling(func_tool=Tool(func_callable=fail_tool), arguments={})
    await event.invoke()
    if event.execution.error is not original or _MARKER not in str(event.execution.error):
        pytest.fail("tool-call validation feedback changed", pytrace=False)


@pytest.mark.asyncio
async def test_hook_supplied_event_keeps_its_own_error_storage():
    from lionagi.service.hooks import HookEventTypes, HookRegistry

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    replacement = APICalling(endpoint=model.endpoint, payload={})
    execution = replacement.execution

    async def replace_event(_event_type, **_kwargs):
        return replacement

    model.hook_registry = HookRegistry(hooks={HookEventTypes.PreEventCreate: replace_event})
    returned = await model.create_event(messages=[{"role": "user", "content": "hello"}])
    original = _private_error()
    returned.execution.error = original
    assert returned is replacement
    assert returned.execution is execution
    assert returned.execution.error is original


@pytest.mark.asyncio
async def test_imodel_custom_event_with_strict_constructor_is_supported():
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    created = []

    class StrictCalling(APICalling):
        def __init__(self):
            super().__init__(endpoint=model.endpoint, payload={})
            created.append(self.execution)

    try:
        call = await model.create_event(create_event_type=StrictCalling)
    except TypeError:
        pytest.fail("custom event constructor did not accept an injected keyword", pytrace=False)
    original = _private_error()
    call.execution.error = original
    if call.execution is not created[0] or call.execution.error is not original:
        pytest.fail("strict custom event did not keep its own execution", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("constructor", ["reject-unknown", "own-execution"])
async def test_custom_kwargs_constructor_keeps_its_original_behavior(constructor):
    from lionagi.protocols.generic import Execution

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    owned = Execution()

    class CustomCalling(APICalling):
        def __init__(self, **kwargs):
            if constructor == "reject-unknown" and kwargs:
                raise TypeError("unexpected keyword")
            if constructor == "own-execution":
                super().__init__(execution=owned, endpoint=model.endpoint, payload={}, **kwargs)
            else:
                super().__init__(endpoint=model.endpoint, payload={}, **kwargs)

    try:
        call = await model.create_event(create_event_type=CustomCalling)
    except TypeError:
        pytest.fail("custom kwargs constructor received an unexpected keyword", pytrace=False)
    original = _private_error()
    call.execution.error = original
    if call.execution.error is not original:
        pytest.fail("custom kwargs constructor did not retain its storage", pytrace=False)
    if constructor == "own-execution":
        assert call.execution is owned


@pytest.mark.asyncio
async def test_custom_constructor_with_declared_execution_is_protected():
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

    class CustomCalling(APICalling):
        def __init__(self, execution=None):
            super().__init__(execution=execution, endpoint=model.endpoint, payload={})

    call = await model.create_event(create_event_type=CustomCalling)
    call.execution.error = _private_error()
    if isinstance(call.execution.error, ValidationError):
        pytest.fail("compatible custom constructor retained raw validation", pytrace=False)


@pytest.mark.asyncio
async def test_custom_constructor_internal_typeerror_is_not_suppressed():
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")

    class BrokenCalling(APICalling):
        def __init__(self, execution=None):
            raise TypeError("constructor failed internally")

    with pytest.raises(TypeError, match="constructor failed internally"):
        await model.create_event(create_event_type=BrokenCalling)


@pytest.mark.asyncio
async def test_explicit_execution_keeps_its_own_error_storage():
    from lionagi.protocols.generic import Execution

    class CustomCalling(APICalling):
        pass

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    execution = Execution()
    call = await model.create_event(
        create_event_type=CustomCalling, endpoint=model.endpoint, payload={}, execution=execution
    )
    original = _private_error()
    call.execution.error = original
    if call.execution is not execution or call.execution.error is not original:
        pytest.fail("explicit execution did not keep its own storage", pytrace=False)


@pytest.mark.asyncio
async def test_rebinding_execution_keeps_caller_owned_error_storage():
    from lionagi.protocols.generic import Execution

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = model.create_api_calling(messages=[{"role": "user", "content": "hello"}])
    replacement = Execution()
    call.execution = replacement
    original = _private_error()
    call.execution.error = original
    if call.execution is not replacement or call.execution.error is not original:
        pytest.fail("rebound execution did not keep caller-owned storage", pytrace=False)


@pytest.mark.asyncio
async def test_external_api_call_keeps_its_own_error_storage(monkeypatch):
    async def succeed(_self):
        return {"ok": True}

    monkeypatch.setattr(APICalling, "_core_invoke", succeed)
    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    call = APICalling(endpoint=model.endpoint, payload={})
    execution = call.execution
    original = _private_error()
    execution.error = original
    try:
        returned = await model.invoke(api_call=call)
    finally:
        await model.close()
    if returned is not call or call.execution is not execution or execution.error is not original:
        pytest.fail("external event did not keep caller-owned storage", pytrace=False)
