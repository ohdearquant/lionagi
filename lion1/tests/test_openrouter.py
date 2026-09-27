import asyncio
import io
import json

import pytest

from hub.harness import OpenRouter, OpenRouterError
from hub.harness import openrouter as mod
from lionagi import Message


def _answers(*replies):
    it = iter(replies)

    def post(body, timeout):
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
    # loop never saw it and the instance ended at rounds=0 with an empty patch
    import http.client

    def cut(req, timeout):
        raise http.client.IncompleteRead(b"x" * 11)

    monkeypatch.setattr(mod.urllib.request, "urlopen", cut)
    monkeypatch.setattr(mod, "api_key", lambda: "k")
    with pytest.raises(OpenRouterError, match="request failed: IncompleteRead") as e:
        mod._post({"model": "m"}, 5.0)
    assert mod._transient(str(e.value))

    def reset(req, timeout):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(mod.urllib.request, "urlopen", reset)
    with pytest.raises(OpenRouterError, match="request failed: ConnectionResetError"):
        mod._post({"model": "m"}, 5.0)


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

    def post(body, timeout):
        seen.append(body)
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 7}},
        }

    monkeypatch.setattr(mod, "_post", post)
    off = OpenRouter(model="m", reasoning={"enabled": False}, stop=["<system round="])
    asyncio.run(off([Message("user", "hi")]))
    assert seen[-1]["reasoning"] == {"enabled": False} and seen[-1]["stop"] == ["<system round="]
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
    # practices-12: a 200 whose body is the provider's error was read as an empty choice and sampled
    # again; its own code says what it is, and a 401 is not transient whatever its prose says
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    posts = []

    def post(body, timeout):
        posts.append(body)
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
        monkeypatch.setattr(mod, "api_key", lambda: "k")
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
    # ported from the practices-12 counter-example: the provider's message was dropped
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(
        mod, "_post", lambda body, timeout: {"error": {"message": "No endpoints found for x/y", "code": 404}}
    )
    with pytest.raises(OpenRouterError) as e:
        asyncio.run(OpenRouter(model="x/y")([]))
    assert "No endpoints found" in str(e.value)


def test_the_reply_carries_the_prompt_count_of_the_attempt_that_answered(monkeypatch):
    # the actor anchors its context figure on this count (ADR-0008 A2); the envelope's bill sums every
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
