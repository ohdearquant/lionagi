# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI-provider request refusals stay private through the iModel boundary."""

import pytest
from pydantic import ValidationError

from lionagi.service.imodel import iModel

_CASES = [
    ("claude_code", "system_prompt_file", False),
    ("codex", "output_schema", False),
    ("gemini_code", "include_directories", True),
    ("pi", "extension", True),
]


def _chain_contains(error, marker):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        values = [str(current), repr(current)]
        if callable(getattr(current, "errors", None)):
            values.append(repr(current.errors()))
        if callable(getattr(current, "json", None)):
            values.append(current.json())
        if any(marker in text for text in values):
            return True
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,field,batch", _CASES, ids=["claude", "codex", "gemini", "pi"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_imodel_path_refusal_keeps_the_entire_exception_chain_private(
    tmp_path, caplog, capsys, provider, field, batch, mode
):
    marker = "sensitive fixture content"
    model = iModel(provider=provider, model="test-model", api_key="test-key")
    value = "../" + marker
    request = {"prompt": "hello", "repo": tmp_path, field: [value] if batch else value}
    error = None
    try:
        if mode == "invoke":
            await model.invoke(**request)
        else:
            async for _ in model.stream(**request):
                pass
    except Exception as caught:
        error = caught
    if error is None:
        pytest.fail("invalid provider path was accepted", pytrace=False)
    if not isinstance(error, ValueError):
        pytest.fail("provider path did not reach its validation boundary", pytrace=False)
    if _chain_contains(error, marker):
        pytest.fail("model boundary exception chain retained input", pytrace=False)
    if "validation error" not in str(error):
        pytest.fail("model boundary omitted its validation count", pytrace=False)
    output = capsys.readouterr()
    if marker in output.out or marker in output.err or marker in caplog.text:
        pytest.fail("model boundary output retained input", pytrace=False)
    assert model.calls_sent == 0


@pytest.mark.parametrize("provider", ["claude_code", "codex", "gemini_code", "pi"])
def test_unrelated_provider_validation_keeps_its_original_error_type(provider):
    model = iModel(provider=provider, model="test-model", api_key="test-key")
    with pytest.raises(ValidationError):
        model.endpoint.create_payload({"prompt": ""})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,field,batch,inline",
    [
        ("claude_code", "system_prompt_file", False, False),
        ("claude_code", "settings", False, True),
        ("codex", "output_schema", False, False),
        ("gemini_code", "include_directories", True, False),
        ("pi", "extension", True, False),
    ],
    ids=["claude-path", "claude-inline", "codex", "gemini", "pi"],
)
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_unrelated_validation_failure_does_not_expose_a_valid_sensitive_field(
    tmp_path, caplog, capsys, provider, field, batch, inline, mode
):
    marker = "sensitive fixture content"
    value = '{"value":"' + marker + '"}' if inline else "folder/" + marker
    model = iModel(provider=provider, model="test-model", api_key="test-key")
    request = {"prompt": "", "repo": tmp_path, field: [value] if batch else value}
    error = None
    try:
        if mode == "invoke":
            await model.invoke(**request)
        else:
            async for _ in model.stream(**request):
                pass
    except Exception as caught:
        error = caught
    if not isinstance(error, ValueError):
        pytest.fail("missing prompt did not reach validation", pytrace=False)
    if _chain_contains(error, marker):
        pytest.fail("unrelated validation failure retained a sensitive field", pytrace=False)
    output = capsys.readouterr()
    if marker in output.out or marker in output.err or marker in caplog.text:
        pytest.fail("validation output retained a sensitive field", pytrace=False)
    assert model.calls_sent == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_claude_model_after_error_does_not_expose_inline_configuration(tmp_path, mode):
    marker = "sensitive fixture content"
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    request = {
        "prompt": "hello",
        "repo": tmp_path,
        "mcp_config": '{"value":"' + marker + '"}',
        "fork_session": True,
    }
    error = None
    try:
        if mode == "invoke":
            await model.invoke(**request)
        else:
            async for _ in model.stream(**request):
                pass
    except Exception as caught:
        error = caught
    if not isinstance(error, ValueError):
        pytest.fail("invalid fork did not reach validation", pytrace=False)
    if _chain_contains(error, marker):
        pytest.fail("model-after validation retained a sensitive field", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["mcp_config", "settings"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_imodel_malformed_inline_configuration_has_a_clean_chain(tmp_path, field, mode):
    marker = "sensitive fixture content"
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    request = {"prompt": "hello", "repo": tmp_path, field: '{"value":"' + marker + '"'}
    error = None
    try:
        if mode == "invoke":
            await model.invoke(**request)
        else:
            async for _ in model.stream(**request):
                pass
    except Exception as caught:
        error = caught
    if not isinstance(error, ValueError):
        pytest.fail("malformed inline configuration was not refused", pytrace=False)
    if _chain_contains(error, marker):
        pytest.fail("inline configuration refusal retained its input", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_unrelated_nonvalidation_event_error_keeps_its_existing_semantics(monkeypatch, mode):
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    original = RuntimeError("ordinary event failure")

    async def fail_event(**_kwargs):
        raise original

    monkeypatch.setattr(model, "create_event", fail_event)
    error = None
    try:
        if mode == "invoke":
            await model.invoke(prompt="hello")
        else:
            async for _ in model.stream(prompt="hello"):
                pass
    except Exception as caught:
        error = caught
    if mode == "invoke":
        assert isinstance(error, ValueError)
        assert error.__cause__ is original
    else:
        assert error is original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_validation_error_details_keep_shape_without_dynamic_values(monkeypatch, mode):
    from pydantic_core import PydanticCustomError

    marker = "sensitive fixture content"
    original = ValidationError.from_exception_data(
        "UnrelatedModel",
        [
            {"type": "int_parsing", "loc": ("payload", marker, 3), "input": marker},
            {
                "type": PydanticCustomError(marker, "invalid value"),
                "loc": (marker,),
                "input": marker,
            },
        ],
    )
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")

    async def fail_event(**_kwargs):
        raise original

    monkeypatch.setattr(model, "create_event", fail_event)
    error = None
    try:
        if mode == "invoke":
            await model.invoke(prompt="hello")
        else:
            async for _ in model.stream(prompt="hello"):
                pass
    except Exception as caught:
        error = caught
    if not isinstance(error, ValueError):
        pytest.fail("validation did not retain the model boundary error type", pytrace=False)
    if _chain_contains(error, marker):
        pytest.fail("dynamic validation location or type retained caller input", pytrace=False)
    text = str(error)
    assert "2 validation errors" in text
    assert "int_parsing" not in text
    assert "<redacted>" not in text


def test_failed_error_count_falls_back_without_rendering_the_exception():
    from lionagi.service.imodel import _safe_validation_details

    class CountFailure:
        def error_count(self):
            raise RuntimeError("private value")

    assert _safe_validation_details(CountFailure()) == "validation failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("raises", [False, True])
async def test_validation_error_subclass_cannot_override_its_count(monkeypatch, mode, raises):
    marker = "sensitive fixture content"

    class UntrustedValidationError(ValidationError):
        def error_count(self):
            if raises:
                raise RuntimeError(marker)
            return marker

    original = UntrustedValidationError.from_exception_data(
        "OtherModel", [{"type": "int_parsing", "loc": (), "input": marker}]
    )
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")

    async def fail_event(**_kwargs):
        raise original

    monkeypatch.setattr(model, "create_event", fail_event)
    caught = None
    try:
        if mode == "invoke":
            await model.invoke(prompt="hello")
        else:
            async for _ in model.stream(prompt="hello"):
                pass
    except Exception as error:
        caught = error
    if not isinstance(caught, ValueError) or _chain_contains(caught, marker):
        pytest.fail("validation count retained caller input", pytrace=False)
    assert "1 validation error" in str(caught)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_explicit_call_validation_is_cleaned_at_the_model_boundary(mode):
    from types import SimpleNamespace

    from lionagi.protocols.generic import EventStatus

    marker = "sensitive fixture content"
    original = ValidationError.from_exception_data(
        "OtherProvider", [{"type": "int_parsing", "loc": (marker, 2), "input": marker}]
    )

    def fail_key_check():
        raise original

    call = SimpleNamespace(
        endpoint=SimpleNamespace(_require_api_key=fail_key_check),
        status=EventStatus.FAILED,
        metadata={},
    )
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    caught = None
    try:
        if mode == "invoke":
            await model.invoke(api_call=call)
        else:
            async for _ in model.stream(api_call=call):
                pass
    except Exception as error:
        caught = error
    assert isinstance(caught, ValueError)
    assert not _chain_contains(caught, marker)
    assert "1 validation error" in str(caught)
    assert "int_parsing" not in str(caught)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_unrenderable_validation_details_have_a_clean_fallback(monkeypatch, capsys, mode):
    import sys

    from pydantic_core import PydanticCustomError

    marker = "sensitive fixture content"

    class Unrenderable:
        def __str__(self):
            raise RuntimeError(marker)

    original = ValidationError.from_exception_data(
        "OtherModel",
        [
            {
                "type": PydanticCustomError("custom", "{bad}", {"bad": Unrenderable()}),
                "loc": (),
                "input": marker,
            }
        ],
    )
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", lambda _event: unraisable.append(True))

    async def fail_event(**_kwargs):
        raise original

    monkeypatch.setattr(model, "create_event", fail_event)
    caught = None
    try:
        if mode == "invoke":
            await model.invoke(prompt="hello")
        else:
            async for _ in model.stream(prompt="hello"):
                pass
    except Exception as error:
        caught = error
    output = capsys.readouterr()
    if (
        not isinstance(caught, ValueError)
        or _chain_contains(caught, marker)
        or unraisable
        or marker in output.out
        or marker in output.err
    ):
        pytest.fail(
            "unrenderable validation details leaked or caused an unraisable error", pytrace=False
        )
    assert "1 validation error" in str(caught)


@pytest.mark.asyncio
async def test_validation_error_in_invoke_cleanup_has_a_clean_chain():
    from types import SimpleNamespace

    from lionagi.protocols.generic import EventStatus

    marker = "sensitive fixture content"
    original = ValidationError.from_exception_data(
        "OtherModel", [{"type": "int_parsing", "loc": (marker,), "input": marker}]
    )

    class BrokenMetadata(dict):
        def pop(self, *_args):
            raise original

    def fail_key_check():
        raise RuntimeError("unrelated error")

    call = SimpleNamespace(
        endpoint=SimpleNamespace(_require_api_key=fail_key_check),
        status=EventStatus.FAILED,
        metadata=BrokenMetadata(),
    )
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    caught = None
    try:
        await model.invoke(api_call=call)
    except Exception as error:
        caught = error
    if not isinstance(caught, ValueError) or _chain_contains(caught, marker):
        pytest.fail("cleanup validation was not safely wrapped", pytrace=False)


@pytest.mark.asyncio
async def test_nonvalidation_in_invoke_cleanup_does_not_retain_validation_input():
    from types import SimpleNamespace

    from lionagi.protocols.generic import EventStatus

    marker = "sensitive fixture content"
    validation = ValidationError.from_exception_data(
        "OtherModel", [{"type": "int_parsing", "loc": (), "input": marker}]
    )
    cleanup_error = RuntimeError("cleanup failed")

    class BrokenMetadata(dict):
        def pop(self, *_args):
            raise cleanup_error

    def fail_key_check():
        raise validation

    call = SimpleNamespace(
        endpoint=SimpleNamespace(_require_api_key=fail_key_check),
        status=EventStatus.FAILED,
        metadata=BrokenMetadata(),
    )
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    caught = None
    try:
        await model.invoke(api_call=call)
    except Exception as error:
        caught = error
    if caught is not cleanup_error or _chain_contains(caught, marker):
        pytest.fail("invoke cleanup exposed input or changed cleanup error", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["start", "pile"])
async def test_nonvalidation_in_stream_cleanup_does_not_retain_validation_input(
    monkeypatch, failure_site
):
    marker = "sensitive fixture content"
    validation = ValidationError.from_exception_data(
        "OtherModel", [{"type": "int_parsing", "loc": (), "input": marker}]
    )
    cleanup_error = RuntimeError("cleanup failed")
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")
    call = await model.create_event(prompt="hello")

    async def fail_step(*_args, **_kwargs):
        raise validation

    def fail_cleanup(*_args):
        raise cleanup_error

    if failure_site == "start":
        monkeypatch.setattr(model.executor, "start", fail_step)
    else:
        monkeypatch.setattr(type(model.executor.pile), "ainclude", fail_step)
    monkeypatch.setattr(model, "_store_served_model", fail_cleanup)
    caught = None
    try:
        async for _ in model.stream(api_call=call):
            pass
    except Exception as error:
        caught = error
    if caught is not cleanup_error or _chain_contains(caught, marker):
        pytest.fail("stream cleanup exposed input or changed cleanup error", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream", "stream-with-limit"])
async def test_validation_never_calls_routing_error_predicate(monkeypatch, mode):
    marker = "sensitive fixture content"
    validation = ValidationError.from_exception_data(
        "OtherModel", [{"type": "int_parsing", "loc": (), "input": marker}]
    )
    model = iModel(
        provider="claude_code",
        model="test-model",
        api_key="test-key",
        concurrency_limit=2 if mode == "stream-with-limit" else None,
    )
    routing_calls = []

    def fail_routing(*_args):
        routing_calls.append(True)
        raise RuntimeError("routing check failed")

    if mode == "invoke":

        async def fail_event(**_kwargs):
            raise validation

        monkeypatch.setattr(model, "create_event", fail_event)
    else:
        call = await model.create_event(prompt="hello")

        async def fail_stream(*_args):
            raise validation
            yield  # pragma: no cover

        monkeypatch.setattr(type(call), "stream", fail_stream)

    monkeypatch.setattr(model, "_is_routing_error", fail_routing)
    caught = None
    try:
        if mode == "invoke":
            await model.invoke(prompt="hello")
        else:
            async for _ in model.stream(api_call=call):
                pass
    except Exception as error:
        caught = error
    finally:
        await model.close()
    if not isinstance(caught, ValueError) or _chain_contains(caught, marker) or routing_calls:
        pytest.fail("routing check exposed a validation input", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("cleanup_kind", ["runtime", "cancel"])
async def test_validation_cleanup_preserves_cleanup_failure_and_clean_chain(
    monkeypatch, mode, cleanup_kind
):
    import asyncio
    from types import SimpleNamespace

    from lionagi.protocols.generic import EventStatus

    marker = "sensitive fixture content"
    validation = ValidationError.from_exception_data(
        "OtherModel", [{"type": "int_parsing", "loc": (), "input": marker}]
    )
    cleanup = (
        RuntimeError("cleanup failed") if cleanup_kind == "runtime" else asyncio.CancelledError()
    )
    model = iModel(provider="claude_code", model="test-model", api_key="test-key")

    def fail_cleanup(*_args):
        raise cleanup

    if mode == "invoke":

        def fail_key_check():
            raise validation

        call = SimpleNamespace(
            endpoint=SimpleNamespace(_require_api_key=fail_key_check),
            status=EventStatus.FAILED,
            metadata={},
        )
    else:
        call = await model.create_event(prompt="hello")

        async def fail_pile(*_args, **_kwargs):
            raise validation

        monkeypatch.setattr(type(model.executor.pile), "ainclude", fail_pile)

    monkeypatch.setattr(model, "_store_served_model", fail_cleanup)
    caught = None
    try:
        if mode == "invoke":
            await model.invoke(api_call=call)
        else:
            async for _ in model.stream(api_call=call):
                pass
    except BaseException as error:
        caught = error
    pending, seen = [caught], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if (
            isinstance(current, ValidationError)
            or marker in str(current)
            or marker in repr(current)
        ):
            pytest.fail("cleanup exception chain retained validation input", pytrace=False)
        pending.extend((current.__cause__, current.__context__))
    if caught is not cleanup:
        pytest.fail("cleanup failure lost its identity or precedence", pytrace=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream", "stream-with-limit"])
async def test_nonvalidation_routing_error_keeps_its_original_identity(monkeypatch, mode):
    model = iModel(
        provider="claude_code",
        model="test-model",
        api_key="test-key",
        concurrency_limit=2 if mode == "stream-with-limit" else None,
    )
    original = RuntimeError("routing error")

    def is_routing(error):
        return error is original

    if mode == "invoke":

        async def fail_event(**_kwargs):
            raise original

        monkeypatch.setattr(model, "create_event", fail_event)
    else:
        call = await model.create_event(prompt="hello")

        async def fail_stream(*_args):
            raise original
            yield  # pragma: no cover

        monkeypatch.setattr(type(call), "stream", fail_stream)
    monkeypatch.setattr(model, "_is_routing_error", is_routing)
    caught = None
    try:
        if mode == "invoke":
            await model.invoke(prompt="hello")
        else:
            async for _ in model.stream(api_call=call):
                pass
    except Exception as error:
        caught = error
    finally:
        await model.close()
    assert caught is original
