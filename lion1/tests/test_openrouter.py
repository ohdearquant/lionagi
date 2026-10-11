import asyncio
import io
import json

import pytest

from hub.backend import OpenRouter, OpenRouterError
from hub.backend import openrouter as mod
from hub.chat import entry_row
from lion1 import Actor, Kind, Message, Profile, RunRequest
from lion1.checkpoint import entry_of
from lion1.lndl import parse

_API_KEY = mod.api_key  # the real lookup, before the fixture stands in for it


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    # resolved once per call, before any request; a test that reads the real key sets its own
    monkeypatch.setattr(mod, "api_key", lambda: "sk-or-made-up")


def _answers(*replies):
    it = iter(replies)

    def post(payload, key, timeout):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    return post


def _ok(text, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": {"cost": 0.001}}


def test_an_errored_choice_is_retried_and_the_attempt_count_recorded(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", _answers(_ok("", finish="error"), _ok("fine")))
    backend = OpenRouter(model="m", retries=2)
    text = asyncio.run(backend([Message("user", "hi")]))
    assert text == "fine"
    assert backend.calls[-1]["attempts"] == 2
    assert backend.calls[-1]["duration_api_ms"] >= 0


def test_a_response_cut_mid_body_is_a_transient_request_failure(monkeypatch):
    # run-2 (2026-09-22): http.client.IncompleteRead escaped _post as itself, so the backend's retry
    # loop never saw it and the instance ended at turns=0 with an empty patch
    import http.client

    def cut(req, timeout):
        raise http.client.IncompleteRead(b"x" * 11)

    monkeypatch.setattr(mod.urllib.request, "urlopen", cut)
    with pytest.raises(OpenRouterError, match="request failed: IncompleteRead") as e:
        mod._post(b"{}", "sk-or-made-up", 5.0)
    assert mod._transient(str(e.value))

    def reset(req, timeout):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(mod.urllib.request, "urlopen", reset)
    with pytest.raises(OpenRouterError, match="request failed: ConnectionResetError"):
        mod._post(b"{}", "sk-or-made-up", 5.0)


def test_a_transient_http_error_is_retried_but_a_client_error_is_not(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", _answers(OpenRouterError("HTTP 503: busy"), _ok("ok")))
    assert asyncio.run(OpenRouter(model="m", retries=1)([Message("user", "hi")])) == "ok"
    monkeypatch.setattr(mod, "_post", _answers(OpenRouterError("HTTP 400: bad"), _ok("never")))
    with pytest.raises(OpenRouterError, match="HTTP 400"):
        asyncio.run(OpenRouter(model="m", retries=3)([Message("user", "hi")]))


def test_a_native_tool_call_answer_is_the_models_turn_as_text_and_is_not_retried(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    tag = 'lact r1>read_file(path="a.py")</lact>'
    calls = [{"id": "c1", "type": "function", "function": {"name": tag, "arguments": "{}"}}]
    reply = {
        "choices": [{"message": {"content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}],
        "usage": {},
    }
    monkeypatch.setattr(mod, "_post", _answers(reply, _ok("never")))
    backend = OpenRouter(model="m", retries=3)
    text = asyncio.run(backend([Message("user", "hi")]))
    assert text == "<" + tag  # the model's `<` is where the provider cut; the loop reads it as a program
    call = backend.calls[-1]
    assert call["attempts"] == 1 and call["finish_reason"] == "tool_calls" and call["tool_calls"] == calls


def test_a_native_tool_call_beside_text_refuses_the_turn_whole_and_the_model_is_told(monkeypatch, store):
    # the call was dropped and the text ran: a turn read by halves
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    calls = [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": '{"cmd": "ls"}'}}]
    mixed = {
        "choices": [
            {
                "message": {"content": "<lvar v1>kept</lvar>", "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    monkeypatch.setattr(mod, "_post", _answers(mixed, _ok("fine"), _ok("plain")))
    backend = OpenRouter(model="m", retries=3)
    actor = Actor("mixed-channels", store=store)
    run = asyncio.run(actor.run(Profile("p"), RunRequest(inputs=("go",), max_turns=2), backend))
    first, _ = backend.calls
    assert first["attempts"] == 1 and first["tool_calls"] == calls  # the model's turn: not sampled again
    turn = run.record.of_kind(Kind.TEXT)[0]
    assert turn.content == "<lvar v1>kept</lvar>" and "two channels" in turn.meta["refused"]
    assert "v1" not in run.values
    notice = next(e.content for e in run.record.of_kind(Kind.SYSTEM) if e.turn == 2)
    assert (
        "your last turn could not be read: the turn came back in two channels, as text and as a native "
        "tool call, and nothing in it was executed" in notice
    )
    # resumed from the record or from the chat log, the refused turn still declares nothing
    for history in (run.record.entries, tuple(entry_of(entry_row(e)) for e in run.record.entries)):
        resumed = asyncio.run(
            actor.run(Profile("p"), RunRequest(history=history, max_turns=1), lambda messages: "ok")
        )
        assert "v1" not in resumed.values
    # a plain answer is not refused
    assert asyncio.run(backend([Message("user", "hi")])).refused is None


def test_the_backend_rewrites_native_markup_and_a_swallowed_closer_before_the_parser(monkeypatch):
    # the parser knows no provider: the markup is read here, in the text and in a rebuilt native call
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    wrapped = (
        "<｜DSML｜ calls>\n"
        '<｜DSML｜ invoke name="lact a9>read_file(path="t/m.py")</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n'
        "</｜DSML｜ calls>"
    )
    swallowed = [{"function": {"name": wrapped.split('name="', 1)[1], "arguments": "{}"}}]
    native = {
        "choices": [{"message": {"content": "", "tool_calls": swallowed}, "finish_reason": "tool_calls"}]
    }
    monkeypatch.setattr(mod, "_post", _answers(_ok(wrapped), native))
    backend = OpenRouter(model="m")
    for _ in range(2):
        text = asyncio.run(backend([Message("user", "hi")]))
        program = parse(text)
        assert "｜DSML｜" not in text and not program.problems
        assert [(la.alias, la.name, la.args) for la in program.lacts] == [
            ("a9", "read_file", {"path": "t/m.py"})
        ]


def test_a_blank_answer_keeps_the_whole_message_for_the_record(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    blank = {
        "choices": [
            {
                "message": {"content": "  \n", "reasoning": "hm"},
                "finish_reason": None,
                "native_finish_reason": "x",
            }
        ],
        "usage": {},
    }
    monkeypatch.setattr(mod, "_post", _answers(blank, _ok("fine")))
    backend = OpenRouter(model="m", retries=2)
    assert asyncio.run(backend([Message("user", "hi")])) == "  \n"  # whitespace is the model's turn
    call = backend.calls[-1]
    assert call["message"] == {"content": "  \n", "reasoning": "hm"} and call["native_finish_reason"] == "x"
    assert asyncio.run(backend([Message("user", "hi")])) == "fine"
    assert "message" not in backend.calls[-1]


def test_a_finished_answer_with_nothing_in_it_is_sampled_again_then_handed_over_blank(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    nothing = {
        "choices": [{"message": {"content": None, "reasoning": None}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 81},
    }
    monkeypatch.setattr(mod, "_post", _answers(nothing, _ok("fine")))
    backend = OpenRouter(model="m", retries=2)
    assert asyncio.run(backend([Message("user", "hi")])) == "fine"
    assert backend.calls[-1]["attempts"] == 2
    monkeypatch.setattr(mod, "_post", _answers(nothing, nothing, nothing, _ok("never")))
    assert asyncio.run(backend([Message("user", "hi")])) == ""  # the loop is told, the run goes on
    call = backend.calls[-1]
    assert call["attempts"] == 3 and call["message"] == {"content": None, "reasoning": None}


def test_retries_exhausted_names_the_count(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", _answers(_ok("", "error"), _ok("", "error")))
    with pytest.raises(OpenRouterError, match="after 2 attempts"):
        asyncio.run(OpenRouter(model="m", retries=1)([Message("user", "hi")]))


class _NoSleep:
    """asyncio with an instant sleep, so the backoff does not slow the suite."""

    @staticmethod
    async def sleep(_seconds):
        return None

    @staticmethod
    async def to_thread(fn, *args):
        return fn(*args)


def test_thinking_off_rides_in_the_body_and_reasoning_tokens_are_read_back(monkeypatch):
    seen = []

    def post(payload, key, timeout):
        seen.append(json.loads(payload))
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 7}},
        }

    monkeypatch.setattr(mod, "_post", post)
    off = OpenRouter(model="m", reasoning={"enabled": False}, stop=["<system turn="])
    asyncio.run(off([Message("user", "hi")]))
    assert seen[-1]["reasoning"] == {"enabled": False} and seen[-1]["stop"] == ["<system turn="]
    assert off.calls[-1]["usage"]["reasoning_output_tokens"] == 7
    asyncio.run(OpenRouter(model="m")([Message("user", "hi")]))
    assert "reasoning" not in seen[-1]  # nothing sent means the provider's default, not a denial


def test_a_sampled_again_answers_bill_is_kept_in_the_envelope(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    nothing = {
        "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80, "cost": 0.25},
    }
    fine = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.10},
    }
    monkeypatch.setattr(mod, "_post", _answers(nothing, fine))
    backend = OpenRouter(model="m", retries=1)
    assert asyncio.run(backend([Message("user", "hi")])) == "ok"
    call = backend.calls[-1]
    assert call["attempts"] == 2 and call["total_cost_usd"] == pytest.approx(0.35)
    assert call["usage"]["output_tokens"] == 90 and call["usage"]["input_tokens"] == 200
    assert [u["cost"] for u in call["attempt_usage"]] == [0.25, 0.10]


def test_a_call_that_fails_after_a_billed_attempt_keeps_the_bill(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    nothing = {
        "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80, "cost": 0.25},
    }
    monkeypatch.setattr(mod, "_post", _answers(nothing, OpenRouterError("HTTP 503: still down")))
    backend = OpenRouter(model="m", retries=1)
    with pytest.raises(OpenRouterError, match="503"):
        asyncio.run(backend([Message("user", "hi")]))
    call = backend.calls[-1]  # the provider billed the blank attempt; the failure keeps that bill
    assert call["error"] == "HTTP 503: still down" and call["attempts"] == 2
    assert call["total_cost_usd"] == pytest.approx(0.25) and call["usage"]["output_tokens"] == 80
    assert [u["cost"] for u in call["attempt_usage"]] == [0.25]


def test_http200_auth_error_is_not_empty_completion_or_retry(monkeypatch):
    # a 200 whose body is the provider's error was read as an empty choice and sampled
    # again; its own code says what it is, and a 401 is not transient whatever its prose says
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    posts = []

    def post(payload, key, timeout):
        posts.append(json.loads(payload))
        return {"error": {"code": 401, "message": "Request timed out while checking the API key"}}

    monkeypatch.setattr(mod, "_post", post)
    backend = OpenRouter(model="m", retries=4)
    with pytest.raises(OpenRouterError) as e:
        asyncio.run(backend([Message("user", "hi")]))
    assert len(posts) == 1
    assert "401: Request timed out while checking the API key" in str(e.value)
    call = backend.calls[-1]  # the answer came back; nothing on it said what it cost
    assert call["attempts"] == 1 and call["error"] == str(e.value)
    assert call["total_cost_usd"] is None and call["cost_complete"] is False


def test_a_refusal_before_any_billed_attempt_lands_an_envelope_with_a_complete_zero(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", _answers(OpenRouterError("HTTP 401: no such key")))
    backend = OpenRouter(model="m", retries=4)
    with pytest.raises(OpenRouterError):
        asyncio.run(backend([Message("user", "hi")]))
    (call,) = backend.calls
    assert call["error"] == "HTTP 401: no such key" and call["attempts"] == 1
    assert call["total_cost_usd"] == 0.0 and call["cost_complete"] is True
    assert call["attempt_usage"] == [] and call["usage"]["prompt_tokens"] == 0


def _billed_blank():
    return {
        "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80, "cost": 0.25},
    }


@pytest.mark.parametrize("second", ["structured", "malformed"])
def test_error_after_billed_response_retains_usage_and_error(monkeypatch, second):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    if second == "structured":
        error = {"error": {"code": 400, "message": "context length exceeded"}, "usage": {"cost": 0.01}}
        monkeypatch.setattr(mod, "_post", _answers(_billed_blank(), error, error, error))
        said, costs, complete = "400: context length exceeded", [0.25, 0.01], True
    else:
        bodies = iter([json.dumps(_billed_blank()).encode(), b"<html>upstream proxy</html>"])

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: Response(next(bodies)))
        said, costs, complete = "unreadable response body: <html>upstream proxy</html>", [0.25, None], False
    backend = OpenRouter(model="m", retries=3 if second == "structured" else 1)
    with pytest.raises(OpenRouterError) as e:
        asyncio.run(backend([Message("user", "hi")]))
    assert said in str(e.value)
    call = backend.calls[-1]
    assert call["error"] == str(e.value) and call["attempts"] == 2
    assert [u.get("cost") for u in call["attempt_usage"]] == costs
    assert call["total_cost_usd"] == pytest.approx(sum(c for c in costs if c is not None))
    assert call["usage"]["output_tokens"] == 80 and call["cost_complete"] is complete


def test_a_transient_error_body_is_retried_and_its_usage_kept(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    busy = {"error": {"code": 502, "message": "upstream busy"}, "usage": {"cost": 0.01}}
    monkeypatch.setattr(mod, "_post", _answers(busy, _ok("fine")))
    backend = OpenRouter(model="m", retries=2)
    assert asyncio.run(backend([Message("user", "hi")])) == "fine"
    call = backend.calls[-1]
    assert call["attempts"] == 2 and [u["cost"] for u in call["attempt_usage"]] == [0.01, 0.001]
    assert call["total_cost_usd"] == pytest.approx(0.011)


def test_the_providers_error_reaches_the_raised_error(monkeypatch):
    # the provider's message was dropped
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(
        mod,
        "_post",
        lambda payload, key, timeout: {"error": {"message": "No endpoints found for x/y", "code": 404}},
    )
    with pytest.raises(OpenRouterError) as e:
        asyncio.run(OpenRouter(model="x/y")([]))
    assert "No endpoints found" in str(e.value)


def test_the_reply_carries_the_prompt_count_of_the_attempt_that_answered(monkeypatch):
    # the actor anchors its context figure on this count (ADR-0009/C2); the envelope's bill sums every
    # attempt, and a sum of two prompts is not the size of either
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    nothing = {
        "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 700, "completion_tokens": 80},
    }
    fine = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 10},
    }
    monkeypatch.setattr(mod, "_post", _answers(nothing, fine))
    backend = OpenRouter(model="m", retries=1)
    text = asyncio.run(backend([Message("user", "hi")]))
    assert text == "ok" and getattr(text, "prompt_tokens", None) == 900
    assert backend.calls[-1]["usage"]["prompt_tokens"] == 1600
    monkeypatch.setattr(mod, "_post", _answers(_ok("again")))
    assert getattr(asyncio.run(backend([Message("user", "hi")])), "prompt_tokens", 0) is None


def test_a_call_cancelled_in_its_backoff_keeps_the_bill_of_the_blank_answer(monkeypatch):
    # a billed blank answer, then a cancel in the sleep before sampling again: the failed envelope
    # was built only on the error paths, so that attempt's cost left no trace on `calls`
    backing_off = asyncio.Event()

    class Blocking(_NoSleep):
        @staticmethod
        async def sleep(_seconds):
            backing_off.set()
            await asyncio.Event().wait()

    blank = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {"cost": 0.002}}
    monkeypatch.setattr(mod, "asyncio", Blocking)
    monkeypatch.setattr(mod, "_post", _answers(blank, _ok("never")))
    backend = OpenRouter(model="m", retries=2)

    async def main():
        task = asyncio.create_task(backend([Message("user", "hi")]))
        await backing_off.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    (call,) = backend.calls
    assert call["error"] == "cancelled" and call["total_cost_usd"] == 0.002 and call["cost_complete"]


def test_a_garbled_status_line_or_an_early_tls_end_is_a_transient_request_failure(monkeypatch):
    # neither is an IncompleteRead or a ConnectionError: both escaped _post as themselves, and the
    # call landed no envelope
    import http.client
    import ssl

    for raised, name in (
        (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), "SSLEOFError"),
        (http.client.BadStatusLine("garbage"), "BadStatusLine"),
    ):

        def fail(req, timeout, raised=raised):
            raise raised

        monkeypatch.setattr(mod.urllib.request, "urlopen", fail)
        with pytest.raises(OpenRouterError, match=f"request failed: {name}") as e:
            mod._post(b"{}", "sk-or-made-up", 5.0)
        assert mod._transient(str(e.value))


def test_a_transport_failure_on_every_attempt_lands_one_envelope_with_its_bill_unknown(monkeypatch):
    import ssl

    def eof(req, timeout):
        raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")

    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod.urllib.request, "urlopen", eof)
    backend = OpenRouter(model="m", retries=1)
    with pytest.raises(OpenRouterError, match="request failed: SSLEOFError"):
        asyncio.run(backend([Message("user", "hi")]))
    (call,) = backend.calls
    assert call["attempts"] == 2 and call["attempt_usage"] == [{}, {}]
    assert call["total_cost_usd"] is None and call["cost_complete"] is False


@pytest.mark.parametrize("where", ["in the request", "after a billed answer"])
def test_an_unexpected_failure_lands_an_envelope_that_names_only_its_class(monkeypatch, where):
    if where == "in the request":

        def post(payload, key, timeout):
            raise RecursionError("a body nested past the parser, from the provider")

        raised, bill, complete = "RecursionError", None, False
    else:
        post = _answers({"choices": {"not": "a list"}, "usage": {"cost": 0.004}})
        raised, bill, complete = "KeyError", 0.004, True
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", post)
    backend = OpenRouter(model="m", retries=3)
    with pytest.raises(OpenRouterError) as e:
        asyncio.run(backend([Message("user", "hi")]))
    assert str(e.value) == f"the response could not be read: {raised}"
    (call,) = backend.calls
    assert call["error"] == str(e.value) and call["attempts"] == 1
    assert call["total_cost_usd"] == bill and call["cost_complete"] is complete


def test_a_call_cancelled_in_its_backoff_after_a_refusal_counts_the_request_it_sent(monkeypatch):
    # a 429 bills nothing, so attempts read off the bills said 0 though one request had gone out
    backing_off = asyncio.Event()

    class Blocking(_NoSleep):
        @staticmethod
        async def sleep(_seconds):
            backing_off.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(mod, "asyncio", Blocking)
    monkeypatch.setattr(mod, "_post", _answers(OpenRouterError("HTTP 429: slow down"), _ok("never")))
    backend = OpenRouter(model="m", retries=2)

    async def main():
        task = asyncio.create_task(backend([Message("user", "hi")]))
        await backing_off.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    (call,) = backend.calls
    assert call["error"] == "cancelled" and call["attempts"] == 1
    assert call["total_cost_usd"] == 0.0 and call["cost_complete"] is True


def test_an_echo_of_the_key_in_a_refusal_reaches_neither_the_error_nor_the_envelope(monkeypatch):
    def refuse(req, timeout):
        body = io.BytesIO(b'{"error": "bad key sk-or-made-up"}')
        raise mod.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(mod, "api_key", lambda: "sk-or-made-up")
    backend = OpenRouter(model="m", retries=0)
    with pytest.raises(OpenRouterError) as e:
        asyncio.run(backend([Message("user", "hi")]))
    assert str(e.value) == 'HTTP 401: {"error": "bad key [key]"}'
    (call,) = backend.calls
    assert call["error"] == str(e.value)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_KEY = "sk-or-v1-" + "0123456789abcdef" * 4


def _no_key_window(text: str) -> bool:
    return not any(_KEY[j : j + 8] in text for j in range(len(_KEY) - 7))


@pytest.mark.parametrize("route", ["across the cut", "unreadable body", "url error", "transport", "answer"])
def test_an_echo_of_the_key_on_any_route_reaches_neither_the_error_nor_the_envelope(monkeypatch, route):
    # only the refusal body was redacted, and only after its cut: a key across the 500th character, an
    # unreadable answer, the transport's text and a readable answer all carried it to the records
    def urlopen(req, timeout):
        if route == "across the cut":
            body = io.BytesIO(("x" * 480 + _KEY).encode())
            raise mod.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)
        if route == "unreadable body":
            return _Response(f"<html>{_KEY}</html>".encode())
        if route == "url error":
            raise mod.urllib.error.URLError(f"proxy said {_KEY}")
        if route == "transport":
            raise ConnectionResetError(104, f"reset after {_KEY}")
        answer = _ok("fine")
        answer["echo"] = {f"seen {_KEY}": [_KEY]}
        return _Response(json.dumps(answer).encode())

    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod, "api_key", lambda: _KEY)
    backend = OpenRouter(model="m", retries=0)
    if route == "answer":
        assert asyncio.run(backend([Message("user", "hi")])) == "fine"
        assert backend.calls[-1]["key_redacted"] is True
    else:
        with pytest.raises(OpenRouterError) as e:
            asyncio.run(backend([Message("user", "hi")]))
        assert _no_key_window(str(e.value)) and "[key]" in str(e.value)
        assert e.value.__context__ is None  # the replaced exception held the provider's text unredacted
    assert _no_key_window(json.dumps(backend.calls))


@pytest.mark.parametrize("echo", [_KEY, _KEY[20:]])
def test_an_echo_across_the_500th_character_is_redacted_before_the_cut(monkeypatch, echo):
    # cut first, an echo starting at the 494th character kept seven of its characters, under the eight
    # a window matches by
    def refuse(req, timeout):
        body = io.BytesIO(("x" * 493 + echo).encode())
        raise mod.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse)
    with pytest.raises(OpenRouterError) as e:
        mod._post(b"{}", _KEY, 5.0)
    assert str(e.value) == "HTTP 401: " + "x" * 493 + "[key]"


def test_an_answer_naming_the_keys_public_start_is_not_rewritten_and_one_past_it_is(monkeypatch):
    said = "Keys look like sk-or-v1-...; the start alone is sk-or-v1"

    def urlopen(req, timeout):
        return _Response(json.dumps(_ok(next(texts))).encode())

    texts = iter([said, f"it was {_KEY[3:20]}"])
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod, "api_key", lambda: _KEY)
    backend = OpenRouter(model="m", retries=0)
    assert asyncio.run(backend([Message("user", "hi")])) == said
    assert "key_redacted" not in backend.calls[-1]
    assert asyncio.run(backend([Message("user", "hi")])) == "it was [key]"
    assert backend.calls[-1]["key_redacted"] is True


def test_a_5xx_whose_body_cannot_be_read_is_still_that_5xx_and_retried(monkeypatch):
    # the body's read raised inside the refusal's handler, so a 503 left as a TimeoutError, unretried
    class Stalled(io.BytesIO):
        def read(self, *a):
            raise TimeoutError("timed out")

    posts = []

    def urlopen(req, timeout):
        posts.append(req)
        if len(posts) < 3:
            raise mod.urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, Stalled())
        return _Response(json.dumps(_ok("fine")).encode())

    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    backend = OpenRouter(model="m", retries=4)
    assert asyncio.run(backend([Message("user", "hi")])) == "fine"
    assert len(posts) == 3 and backend.calls[-1]["attempts"] == 3

    def stalled(req, timeout):
        raise mod.urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, Stalled())

    monkeypatch.setattr(mod.urllib.request, "urlopen", stalled)
    with pytest.raises(OpenRouterError, match=r"^HTTP 503: \(the body could not be read: TimeoutError\)$"):
        mod._post(b"{}", "sk-or-made-up", 5.0)


@pytest.mark.parametrize("shape", ["content a list", "a count as text", "a tool call as text", "a vast cost"])
def test_an_answer_in_a_shape_the_call_cannot_read_is_a_failed_attempt_that_lands_its_envelope(
    monkeypatch, shape
):
    # the envelope was built after the handlers, so a shape that broke it raised with none landed
    answer = _ok("fine")
    if shape == "content a list":
        answer["choices"][0]["message"]["content"] = ["fine"]
    elif shape == "a count as text":
        answer["usage"]["prompt_tokens"] = "12"
    elif shape == "a tool call as text":
        answer["choices"][0]["message"] = {"content": None, "tool_calls": ["run"]}
    else:
        answer["usage"]["cost"] = 10**400
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda req, timeout: _Response(json.dumps(answer).encode())
    )
    backend = OpenRouter(model="m", retries=1)
    with pytest.raises(OpenRouterError, match="request failed: unreadable response body"):
        asyncio.run(backend([Message("user", "hi")]))
    (call,) = backend.calls
    assert call["attempts"] == 2 and call["total_cost_usd"] is None and call["cost_complete"] is False

    # past the request, an answer that still breaks the reading lands one envelope with its bill
    monkeypatch.setattr(mod, "_post", _answers({**answer, "usage": {"cost": 0.002}}))
    backend = OpenRouter(model="m", retries=1)
    if shape == "content a list":
        with pytest.raises(OpenRouterError, match="^the response could not be read: AttributeError$"):
            asyncio.run(backend([Message("user", "hi")]))
        (call,) = backend.calls
        assert call["attempts"] == 1 and call["total_cost_usd"] == 0.002 and call["cost_complete"] is True


def test_a_vast_cost_on_a_blank_answer_is_unread_and_the_retry_lands_one_envelope(monkeypatch):
    # an integer cost of 400 digits passed, and summed with the retry's float it raised in the call and
    # again in its failure path, so no envelope landed
    blank = _ok("")
    blank["usage"]["cost"] = 10**400
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    answers = iter([blank, _ok("fine")])
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda req, timeout: _Response(json.dumps(next(answers)).encode())
    )
    backend = OpenRouter(model="m", retries=1)
    assert asyncio.run(backend([Message("user", "hi")])) == "fine"
    (call,) = backend.calls
    assert call["attempts"] == 2 and call["total_cost_usd"] == 0.001 and call["cost_complete"] is False


@pytest.mark.parametrize(
    "refused", ["no key", "a key the header cannot take", "a body that cannot be sent", "a malformed view"]
)
def test_a_call_refused_before_any_request_lands_a_known_zero_with_no_attempt(monkeypatch, refused):
    # the key was resolved and the body encoded inside the request, so the envelope counted one
    def urlopen(req, timeout):
        raise AssertionError("no request goes out")

    def missing():
        raise OpenRouterError("OPENROUTER_API_KEY: not in the environment and not in the keychain")

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    if refused == "no key":
        monkeypatch.setattr(mod, "api_key", missing)
        backend, said = (
            OpenRouter(model="m"),
            "OPENROUTER_API_KEY: not in the environment and not in the keychain",
        )
    elif refused == "a key the header cannot take":
        # a trailing newline failed inside the request, which counted it as sent
        monkeypatch.setattr(mod, "api_key", _API_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY", _KEY + "\n")
        backend, said = OpenRouter(model="m"), f"the OpenRouter key is not {mod._KEY_FORM}"
    elif refused == "a body that cannot be sent":
        backend, said = (
            OpenRouter(model="m", provider={"order": object()}),
            "the request could not be built: TypeError",
        )
    else:
        # the body was built before the preflight, so a view it could not read raised with no envelope
        backend, said = OpenRouter(model="m"), "the request could not be built: AttributeError"
    view = [object()] if refused == "a malformed view" else [Message("user", "hi")]
    with pytest.raises(OpenRouterError) as e:
        asyncio.run(backend(view))
    assert str(e.value) == said and e.value.__context__ is None
    (call,) = backend.calls
    assert call["attempts"] == 0 and call["total_cost_usd"] == 0.0 and call["cost_complete"] is True
