import asyncio
import base64
import http.client
import json
import threading
import urllib.parse

import pytest

from hub.backend import Anthropic, AnthropicError
from hub.backend import anthropic as mod
from lion1 import Message


@pytest.fixture(autouse=True)
def _made_up_credentials(monkeypatch, tmp_path):
    """Every test runs under a made-up environment key and a stand-in keychain tool answering any account
    with a made-up item: the key is read before the first request, and no test reaches the real tool."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-made-up-key-0123456789")
    tool = tmp_path / "security-stand-in"
    tool.write_text('#!/bin/sh\nprintf "sk-ant-made-up-item-0123456789\\n"\n')
    tool.chmod(0o755)
    monkeypatch.setattr(mod, "_SECURITY", str(tool))


class _NoSleep:
    """asyncio with an instant sleep, so the backoff does not slow the suite."""

    @staticmethod
    async def sleep(_seconds):
        return None

    @staticmethod
    async def to_thread(fn, *args):
        return fn(*args)


def _usage(inp=4, read=0, w5=0, w1h=0, out=3, model="claude-haiku-5-5"):
    return {
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": w5 + w1h,
        "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1h},
        "output_tokens": out,
        "model": model,
    }


def _ok(text, stop="end_turn", usage=None, blocks=None):
    u = dict(usage or _usage())
    model = u.pop("model")
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    return {"id": "msg_1", "model": model, "content": content, "stop_reason": stop, "usage": u}


def _answers(*replies, headers=None, bodies=None):
    it = iter(replies)

    def post(body, timeout, account, heard=None):
        if bodies is not None:
            bodies.append(body)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r, dict(headers or {})

    return post


def _marks(body):
    """Where the breakpoints are: 'system' and the index of each marked message."""
    out = []
    for block in body.get("system") or []:
        if "cache_control" in block:
            out.append("system")
    for i, m in enumerate(body["messages"]):
        if any("cache_control" in b for b in m["content"]):
            out.append(i)
    return out


def test_the_head_is_system_with_a_breakpoint_and_the_tail_is_marked(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    bodies = []
    monkeypatch.setattr(mod, "_post", _answers(_ok("fine"), bodies=bodies))
    backend = Anthropic(model="claude-haiku-5-5", stop=["<system turn="])
    view = [
        Message("system", "the head"),
        Message("user", "[j1] go", name="j1"),
        Message("assistant", "<lact a1>run()</lact>", name="t1"),
        Message("system", "<system turn=2>notice</system>", name="n2"),
    ]
    assert asyncio.run(backend(view)) == "fine"
    (body,) = bodies
    assert body["system"] == [
        {"type": "text", "text": "the head", "cache_control": {"type": "ephemeral", "ttl": "5m"}}
    ]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]  # a notification as user
    assert _marks(body) == ["system", 2]
    assert body["stop_sequences"] == ["<system turn="] and body["max_tokens"] == 32768
    assert "thinking" not in body and "temperature" not in body and "tools" not in body


def test_the_previous_calls_tail_is_pinned_while_the_view_holds_it_and_never_more_than_three_marks(
    monkeypatch,
):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    bodies = []
    monkeypatch.setattr(mod, "_post", _answers(_ok("a"), _ok("b"), _ok("c"), bodies=bodies))
    backend = Anthropic(model="claude-haiku-5-5", ttl="1h")
    head = Message("system", "the head")
    first = [head, Message("user", "[j1] go", name="j1")]
    asyncio.run(backend(first))
    # the next turn adds many results: the previous tail keeps its mark, the new tail gets one
    second = [*first, Message("assistant", "turn", name="t1")] + [
        Message("user", f"[r{i}] ok", name=f"r{i}") for i in range(25)
    ]
    asyncio.run(backend(second))
    # the fold replaced the previous tail with a summary under its own name: the bytes changed, no pin
    third = [
        head,
        Message("user", "[summary before r25] folded", name="r24", folded=True),
        Message("user", "[j2] on", name="j2"),
    ]
    asyncio.run(backend(third))
    one, two, three = bodies
    assert _marks(one) == ["system", 0]
    assert _marks(two) == ["system", 0, 26] and len(two["messages"]) == 27
    assert _marks(three) == ["system", 1]
    for body in bodies:
        marks = [b["cache_control"] for m in body["messages"] for b in m["content"] if "cache_control" in b]
        assert all(m == {"type": "ephemeral", "ttl": "1h"} for m in marks)
        assert len(_marks(body)) <= 3


def test_an_empty_entry_is_sent_as_a_placeholder_block_and_a_view_without_a_message_is_refused(monkeypatch):
    bodies = []
    monkeypatch.setattr(mod, "_post", _answers(_ok("x"), bodies=bodies))
    backend = Anthropic(model="claude-haiku-5-5")
    asyncio.run(
        backend(
            [
                Message("system", "h"),
                Message("assistant", "  ", name="t1"),
                Message("user", "[j] go", name="j"),
            ]
        )
    )
    assert bodies[0]["messages"][0]["content"] == [{"type": "text", "text": "[empty]"}]
    with pytest.raises(AnthropicError, match="no message after the head"):
        asyncio.run(backend([Message("system", "h")]))
    with pytest.raises(ValueError, match="ttl is one of 5m, 1h"):
        Anthropic(model="m", ttl="2h")


def test_the_count_is_the_answering_attempts_three_input_counts_and_usage_sums_every_attempt(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    blank = _ok("", blocks=[], usage=_usage(inp=5, read=900, w5=20, out=1))
    fine = _ok("fine", usage=_usage(inp=4, read=1000, w1h=30, out=7))
    monkeypatch.setattr(mod, "_post", _answers(blank, fine))
    backend = Anthropic(model="claude-haiku-5-5", retries=2)
    reply = asyncio.run(backend([Message("user", "hi", name="j")]))
    assert reply == "fine" and reply.prompt_tokens == 4 + 1000 + 30
    call = backend.calls[-1]
    assert call["attempts"] == 2
    assert call["usage"] == {
        "prompt_tokens": 9 + 1900 + 50,
        "completion_tokens": 8,
        "input_tokens": 9,
        "cache_read_input_tokens": 1900,
        "cache_creation_input_tokens": 50,
        "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30},
        "output_tokens": 8,
        "reasoning_output_tokens": None,
    }
    assert call["finish_reason"] == "stop" and call["native_finish_reason"] == "end_turn"
    assert call["cost_complete"] is True and len(call["attempt_usage"]) == 2


def test_cost_is_the_dated_table_at_the_requests_tier_and_unknown_for_a_model_it_lacks(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    under = _ok("a", usage=_usage(inp=1000, read=50_000, w5=2000, w1h=1000, out=500))
    over = _ok("b", usage=_usage(inp=1000, read=150_000, w5=2000, out=500))
    other = _ok("c", usage=_usage(model="claude-next-9"))
    monkeypatch.setattr(mod, "_post", _answers(under, over, other))
    backend = Anthropic(model="claude-haiku-5-5")
    view = [Message("user", "hi", name="j")]
    asyncio.run(backend(view))
    asyncio.run(backend(view))
    asyncio.run(backend(view))
    a, b, c = backend.calls
    assert a["tier"] == "up to 100000"
    assert a["total_cost_usd"] == pytest.approx(
        (1000 * 0.10 + 2000 * 0.125 + 1000 * 0.20 + 50_000 * 0.01 + 500 * 0.50) / 1e6
    )
    assert b["tier"] == "over 100000"
    assert b["total_cost_usd"] == pytest.approx(
        (1000 * 0.50 + 2000 * 0.625 + 150_000 * 0.05 + 500 * 2.50) / 1e6
    )
    assert c["total_cost_usd"] is None and c["cost_complete"] is False and c["tier"] is None
    assert mod.priced("claude-sonnet-5-5") and not mod.priced("claude-next-9")


def test_a_429_waits_its_retry_after_and_a_5xx_backs_off_but_a_401_is_not_retried(monkeypatch):
    waited = []

    class _Sleep(_NoSleep):
        @staticmethod
        async def sleep(seconds):
            waited.append(seconds)

    monkeypatch.setattr(mod, "asyncio", _Sleep)
    busy = AnthropicError("HTTP 429: slow down", status=429, retry_after=7.0)
    down = AnthropicError("HTTP 529: overloaded", status=529)
    monkeypatch.setattr(mod, "_post", _answers(busy, down, _ok("ok")))
    backend = Anthropic(model="claude-haiku-5-5", retries=3)
    assert asyncio.run(backend([Message("user", "hi", name="j")])) == "ok"
    assert waited == [7.0, 4.0] and backend.calls[-1]["attempts"] == 3
    monkeypatch.setattr(
        mod, "_post", _answers(AnthropicError("HTTP 401: invalid x-api-key", status=401), _ok("never"))
    )
    with pytest.raises(AnthropicError, match="HTTP 401"):
        asyncio.run(Anthropic(model="m", retries=3)([Message("user", "hi", name="j")]))
    assert mod._wait(0, 10_000.0) == mod._RETRY_AFTER_MAX and mod._wait(2, None) == 8.0


def test_a_refusal_before_any_billed_attempt_lands_an_envelope_with_a_complete_zero(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", _answers(AnthropicError("HTTP 401: invalid x-api-key", status=401)))
    backend = Anthropic(model="claude-haiku-5-5", account="anthropic_api_key_b")
    with pytest.raises(AnthropicError, match="HTTP 401"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert failed["error"].startswith("HTTP 401") and failed["attempts"] == 1
    assert failed["total_cost_usd"] == 0.0 and failed["cost_complete"] is True
    assert failed["usage"]["prompt_tokens"] is None and failed["account"] == "anthropic_api_key_b"
    assert failed["usage_complete"] is True  # nothing was sent: the usage is whole, and empty
    # sent and nothing readable came back: billed or not is unknown, so the cost is
    monkeypatch.setattr(mod, "_post", _answers(AnthropicError("request failed: timed out")))
    with pytest.raises(AnthropicError, match="request failed"):
        asyncio.run(Anthropic(model="claude-haiku-5-5", retries=0)([Message("user", "hi", name="j")]))


def test_a_response_without_usage_has_no_count_and_no_cost(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    bare = {
        "id": "m",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "end_turn",
    }
    monkeypatch.setattr(mod, "_post", _answers(bare))
    backend = Anthropic(model="claude-haiku-5-5")
    reply = asyncio.run(backend([Message("user", "hi", name="j")]))
    assert reply == "x" and reply.prompt_tokens is None
    call = backend.calls[-1]
    assert call["total_cost_usd"] is None and call["cost_complete"] is False and call["tier"] is None
    assert call["usage"]["prompt_tokens"] == call["usage"]["output_tokens"] is None  # no figure, not zero
    assert call["usage_complete"] is False


def test_a_count_the_response_lacks_is_unknown_and_a_bill_with_one_is_incomplete(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    short = _usage(inp=4, out=3)
    del short["output_tokens"]
    monkeypatch.setattr(mod, "_post", _answers(_ok("x", usage=short)))
    backend = Anthropic(model="claude-haiku-5-5")
    reply = asyncio.run(backend([Message("user", "hi", name="j")]))
    assert reply.prompt_tokens == 4  # the three input counts came
    call = backend.calls[-1]
    assert call["usage"]["input_tokens"] == 4 and call["usage"]["output_tokens"] is None
    assert call["total_cost_usd"] is None and call["cost_complete"] is False
    assert call["usage_complete"] is False
    # a count that is not a count is unknown too, and a zero is a zero
    odd = _usage(inp=4, out=0)
    odd["cache_read_input_tokens"] = "7"
    monkeypatch.setattr(mod, "_post", _answers(_ok("x", usage=odd)))
    reply = asyncio.run(backend([Message("user", "hi", name="j")]))
    assert reply.prompt_tokens is None and backend.calls[-1]["usage"]["output_tokens"] == 0
    assert backend.calls[-1]["cost_complete"] is False


def test_a_write_the_response_does_not_split_is_priced_at_the_requests_ttl(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    unsplit = _usage(inp=0, w5=1000)
    del unsplit["cache_creation"]
    for ttl, price in (("1h", 0.20), ("5m", 0.125)):
        monkeypatch.setattr(mod, "_post", _answers(_ok("x", usage=unsplit)))
        backend = Anthropic(model="claude-haiku-5-5", ttl=ttl)
        asyncio.run(backend([Message("user", "hi", name="j")]))
        call = backend.calls[-1]
        assert call["total_cost_usd"] == pytest.approx((1000 * price + 3 * 0.50) / 1e6)
        assert call["cost_complete"] and call["usage"]["cache_creation_input_tokens"] == 1000
        split = {"ephemeral_5m_input_tokens": None, "ephemeral_1h_input_tokens": None}
        assert call["usage"]["cache_creation"] == split
    # a split that does not sum to the write it splits, or half a split, certifies nothing: the bill
    # stays open under either TTL
    k5, k1h = "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"
    splits = [{k5: 0, k1h: 0}, {k5: 300, k1h: 300}, {k5: 700, k1h: 700}, {k1h: 1200}, {k1h: 1000}, {k5: 0}]
    # a split the response carries but that is not a count is read as a split, never as none: the
    # bill stays open, not priced at the request's TTL
    splits += [{k5: -1, k1h: -1}, {k5: "1000", k1h: "0"}, {k5: True, k1h: 999}, {}, None]
    for ttl in ("1h", "5m"):
        for split in splits:
            odd = _usage(inp=0, w5=0, w1h=0)
            odd["cache_creation_input_tokens"] = 1000
            odd["cache_creation"] = split
            monkeypatch.setattr(mod, "_post", _answers(_ok("x", usage=odd)))
            backend = Anthropic(model="claude-haiku-5-5", ttl=ttl)
            asyncio.run(backend([Message("user", "hi", name="j")]))
            call = backend.calls[-1]
            assert call["total_cost_usd"] is None and call["cost_complete"] is False, (ttl, split)
            assert call["usage"]["cache_creation_input_tokens"] == 1000
    # the split, when it comes, is the price whatever the request asked
    monkeypatch.setattr(mod, "_post", _answers(_ok("x", usage=_usage(inp=0, w1h=1000))))
    backend = Anthropic(model="claude-haiku-5-5", ttl="5m")
    asyncio.run(backend([Message("user", "hi", name="j")]))
    assert backend.calls[-1]["total_cost_usd"] == pytest.approx((1000 * 0.20 + 3 * 0.50) / 1e6)


def test_a_view_refused_before_any_request_lands_an_envelope_with_zero_attempts(monkeypatch):
    sent = []
    monkeypatch.setattr(mod, "_post", _answers(_ok("x"), bodies=sent))
    backend = Anthropic(model="claude-haiku-5-5", account="anthropic_api_key_c")
    with pytest.raises(AnthropicError, match="no message after the head"):
        asyncio.run(backend([Message("system", "h")]))
    (failed,) = backend.calls
    assert failed["attempts"] == 0 and sent == []
    assert failed["error"].startswith("the view has no message")
    assert failed["account"] == "anthropic_api_key_c"
    assert failed["total_cost_usd"] == 0.0 and failed["cost_complete"] is True and failed["limits"] == {}


def test_a_call_that_fails_after_a_billed_attempt_keeps_the_bill_with_its_account(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    blank = _ok("", blocks=[], usage=_usage(inp=4, out=2))
    refusal = AnthropicError("HTTP 400: bad", status=400, headers={"request-id": "req_400", "date": "x"})
    monkeypatch.setattr(mod, "_post", _answers(blank, refusal))
    backend = Anthropic(model="claude-haiku-5-5", retries=2, account="anthropic_api_key_a")
    with pytest.raises(AnthropicError, match="HTTP 400"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert failed["error"] == "HTTP 400: bad" and failed["attempts"] == 2
    assert failed["limits"] == {"request-id": "req_400"}  # the refusing response's, kept
    assert failed["usage"]["output_tokens"] == 2 and failed["account"] == "anthropic_api_key_a"
    # the refused second attempt billed nothing: the one billed attempt is the whole, known bill
    assert failed["total_cost_usd"] == pytest.approx((4 * 0.10 + 2 * 0.50) / 1e6)
    assert failed["cost_complete"] is True and failed["attempt_usage"][0]["output_tokens"] == 2
    # an unreadable last response leaves its own headers on the envelope, not an earlier attempt's
    cut = AnthropicError(
        "request failed: unreadable response body: <html>", headers={"request-id": "req_cut"}
    )
    monkeypatch.setattr(mod, "_post", _answers(blank, cut))
    backend = Anthropic(model="claude-haiku-5-5", retries=1)
    with pytest.raises(AnthropicError, match="unreadable"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert failed["limits"] == {"request-id": "req_cut"} and failed["cost_complete"] is False


def test_a_tool_use_block_alone_is_the_turn_as_text_and_beside_text_refuses_the_turn(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    use = {"type": "tool_use", "id": "tu_1", "name": "run", "input": {"cmd": "ls"}}
    alone = _ok("", stop="tool_use", blocks=[use])
    mixed = _ok("", stop="tool_use", blocks=[{"type": "text", "text": "<lvar v1>kept</lvar>"}, use])
    monkeypatch.setattr(mod, "_post", _answers(alone, mixed))
    backend = Anthropic(model="claude-haiku-5-5", retries=3)
    text = asyncio.run(backend([Message("user", "hi", name="j")]))
    assert text.startswith("<lact auto_") and text.endswith(">run(cmd='ls')</lact>") and text.refused is None
    assert backend.calls[-1]["attempts"] == 1 and backend.calls[-1]["tool_calls"] == [use]
    assert backend.calls[-1]["finish_reason"] == "tool_calls"
    both = asyncio.run(backend([Message("user", "hi", name="j")]))
    assert both == "<lvar v1>kept</lvar>" and "two channels" in both.refused


def test_a_turn_cut_at_the_output_cap_reads_length_and_thinking_blocks_are_not_kept(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    cut = _ok(
        "",
        stop="max_tokens",
        blocks=[{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "<lact a1>run("}],
        usage={**_usage(out=40), "output_tokens_details": {"thinking_tokens": 30}},
    )
    bodies = []
    monkeypatch.setattr(mod, "_post", _answers(cut, bodies=bodies))
    backend = Anthropic(model="claude-haiku-5-5", effort="high", retries=2)
    assert asyncio.run(backend([Message("user", "hi", name="j")])) == "<lact a1>run("
    call = backend.calls[-1]
    assert call["finish_reason"] == "length" and call["attempts"] == 1
    assert call["usage"]["reasoning_output_tokens"] == 30
    # the current models take adaptive thinking under an effort; a token budget is refused
    assert bodies[0]["thinking"] == {"type": "adaptive"} and bodies[0]["output_config"] == {"effort": "high"}
    plain = Anthropic(model="claude-haiku-5-5").body([Message("user", "hi", name="j")])
    assert "thinking" not in plain and "output_config" not in plain
    # a capped turn that wrote only thinking is handed back blank; its content is kept, less the thinking
    only = _ok(
        "",
        stop="max_tokens",
        blocks=[{"type": "thinking", "thinking": "private"}, {"type": "text", "text": ""}],
    )
    monkeypatch.setattr(mod, "_post", _answers(only))
    backend = Anthropic(model="claude-haiku-5-5", effort="high", retries=0)
    assert asyncio.run(backend([Message("user", "hi", name="j")])) == ""
    assert backend.calls[-1]["message"] == [{"type": "text", "text": ""}]
    assert "private" not in json.dumps(backend.calls)


def test_a_blank_answer_is_sampled_again_then_handed_over_blank_with_its_content_kept(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    nothing = _ok("", blocks=[])
    monkeypatch.setattr(mod, "_post", _answers(nothing, nothing, _ok("never")))
    backend = Anthropic(model="claude-haiku-5-5", retries=1)
    assert asyncio.run(backend([Message("user", "hi", name="j")])) == ""
    assert backend.calls[-1]["attempts"] == 2 and backend.calls[-1]["message"] == []


def test_a_body_cut_mid_read_keeps_its_own_headers_on_the_failed_envelope(monkeypatch):
    class Cut:
        headers = {"request-id": "req_cut", "anthropic-ratelimit-requests-remaining": "7"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @staticmethod
        def read():
            raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: Cut())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "made-up-key-01")
    with pytest.raises(AnthropicError, match="IncompleteRead"):
        mod._post({"model": "m"}, 1.0, "made-up-key-01")
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    backend = Anthropic(model="claude-haiku-5-5", retries=0)
    with pytest.raises(AnthropicError, match="IncompleteRead"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert failed["limits"] == {"request-id": "req_cut", "anthropic-ratelimit-requests-remaining": "7"}
    assert failed["cost_complete"] is False and failed["attempts"] == 1


def test_a_refusal_whose_body_is_cut_mid_read_is_still_that_refusal_with_its_headers(monkeypatch):
    import email.message
    import urllib.error

    def cut(code, request_id):
        hdrs = email.message.Message()
        hdrs["request-id"] = request_id
        hdrs["Retry-After"] = "0"

        class Cut(urllib.error.HTTPError):
            def read(self, *a):
                raise http.client.IncompleteRead(b"{")

        return Cut(mod.URL, code, "cut", hdrs, None)

    class Blank:
        headers = {"request-id": "req_blank"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @staticmethod
        def read():
            return json.dumps(_ok("", blocks=[])).encode()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "made-up-key-01")
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    # first attempt: the refusal keeps its status, so a 401 is not retried and its headers ride
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: _raise(cut(401, "req_401")))
    backend = Anthropic(model="claude-haiku-5-5", retries=2)
    with pytest.raises(AnthropicError, match="HTTP 401: \\(the body could not be read: IncompleteRead\\)"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert failed["limits"] == {"request-id": "req_401"} and failed["attempts"] == 1
    assert failed["total_cost_usd"] == 0.0 and failed["cost_complete"] is True
    # after a billed blank answer: the 503's cut body ends the call with the bill kept and the 503 named
    answers = iter([Blank(), cut(503, "req_503")])

    def urlopen(req, timeout):
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    backend = Anthropic(model="claude-haiku-5-5", retries=1)
    with pytest.raises(AnthropicError, match="HTTP 503"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert failed["limits"] == {"request-id": "req_503"} and failed["attempts"] == 2
    assert failed["usage"]["prompt_tokens"] == 4 and failed["cost_complete"] is True
    assert len(failed["attempt_usage"]) == 1  # a refusal is not a billed attempt


def _raise(e):
    raise e


def test_the_limits_and_request_id_ride_the_envelope(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    headers = {
        "anthropic-ratelimit-input-tokens-remaining": "1999",
        "anthropic-ratelimit-requests-limit": "1000",
        "request-id": "req_1",
        "content-type": "application/json",
    }
    monkeypatch.setattr(mod, "_post", _answers(_ok("x"), headers=headers))
    backend = Anthropic(model="claude-haiku-5-5")
    asyncio.run(backend([Message("user", "hi", name="j")]))
    assert backend.calls[-1]["limits"] == {
        "anthropic-ratelimit-input-tokens-remaining": "1999",
        "anthropic-ratelimit-requests-limit": "1000",
        "request-id": "req_1",
    }


def test_the_key_comes_from_the_environment_without_an_account_and_from_the_item_with_one(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AnthropicError, match="not in the environment"):
        mod.api_key(None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-0123456789")
    assert mod.api_key(None) == "sk-ant-env-0123456789"
    # with an account the environment is not read: the item is, by the pool's name on the tool's argv
    tool = tmp_path / "security"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$0.argv"\nprintf "sk-ant-item-0123456789\\n"\n')
    tool.chmod(0o755)
    monkeypatch.setattr(mod, "_SECURITY", str(tool))
    assert mod.api_key("anthropic_api_key_a") == "sk-ant-item-0123456789"
    assert (
        tmp_path / "security.argv"
    ).read_text().strip() == "find-generic-password -a anthropic_api_key_a -s ANTHROPIC_API_KEY -w"
    with pytest.raises(AnthropicError, match="letters, digits"):
        mod.api_key("a b; rm")
    assert not (tmp_path / "security.argv").read_text().count("rm")  # refused before the tool ran
    tool.write_text("#!/bin/sh\nexit 44\n")
    with pytest.raises(AnthropicError, match="no keychain item"):
        mod.api_key("anthropic_api_key_b")


def test_the_key_rides_only_the_request_header_and_is_in_no_envelope_or_error(monkeypatch):
    seen = {}

    class _Resp:
        headers = {"request-id": "r"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(_ok("hi")).encode()

    def urlopen(req, timeout):
        seen["headers"] = dict(req.headers)
        seen["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-made-up-key-0123456789")
    backend = Anthropic(model="claude-haiku-5-5")
    asyncio.run(backend([Message("system", "h"), Message("user", "[j] go", name="j")]))
    assert seen["headers"]["X-api-key"] == "sk-ant-made-up-key-0123456789"
    assert "sk-ant-made-up" not in seen["body"]
    assert "sk-ant-made-up" not in json.dumps(backend.calls) and "sk-ant-made-up" not in repr(backend)

    def refuse(req, timeout):
        import urllib.error

        raise urllib.error.HTTPError(mod.URL, 401, "x", {"retry-after": "3"}, None)

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse)
    with pytest.raises(AnthropicError, match="HTTP 401") as e:
        mod._post({"model": "m"}, 5.0, "sk-ant-made-up-key-0123456789")
    assert e.value.status == 401 and e.value.retry_after == 3.0
    assert "sk-ant-made-up" not in str(e.value)


def _respond(monkeypatch, *responses):
    """`urlopen` stand-ins, in order: a response object, or an exception to raise."""
    it = iter(responses)

    def urlopen(req, timeout):
        nxt = next(it)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)


def _http_error(code, body, headers=None):
    import email.message
    import io
    import urllib.error

    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError(mod.URL, code, "x", hdrs, io.BytesIO(body.encode()))


class _Body:
    headers = {"request-id": "r"}

    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.raw.encode()


def _windows(key):
    return [key[i : i + 8] for i in range(len(key) - 7)]


def test_a_provider_body_echoing_the_key_is_redacted_from_the_error_and_the_envelope(monkeypatch):
    key = "sk-ant-made-up-key-0123456789abcdef"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    echo = json.dumps({"error": {"message": f"bad key {key}; and a part of it: {key[10:30]}"}})
    _respond(monkeypatch, _http_error(400, echo, {"request-id": "req_echo"}))
    backend = Anthropic(model="claude-haiku-5-5", retries=0)
    with pytest.raises(AnthropicError, match="HTTP 400") as e:
        asyncio.run(backend([Message("user", "hi", name="j")]))
    written = str(e.value) + json.dumps(backend.calls)
    assert "[key]" in str(e.value) and backend.calls[-1]["limits"] == {"request-id": "req_echo"}
    assert not any(w in written for w in _windows(key))
    # a 200 whose body is unreadable and echoes it: the same
    _respond(monkeypatch, _Body(f"<html>{key}</html>"))
    backend = Anthropic(model="claude-haiku-5-5", retries=0)
    with pytest.raises(AnthropicError, match="unreadable") as e:
        asyncio.run(backend([Message("user", "hi", name="j")]))
    assert not any(w in str(e.value) + json.dumps(backend.calls) for w in _windows(key))
    assert mod._redact("abcdefghij", "abcdefghij") == "[key]" and mod._redact("x", "short") == "x"


def test_a_key_in_a_header_name_is_redacted_before_the_name_is_lowered_and_in_any_case(monkeypatch):
    key = "sk-ant-Made-UP-key-0123456789ABCDEF"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(mod, "asyncio", _NoSleep)

    class _Named(_Body):
        headers = {
            "Request-Id": "req_named",
            f"Anthropic-RateLimit-{key}": "7",  # as sent
            f"anthropic-ratelimit-folded-{key.lower()}": "8",  # case-folded by a hop
            f"anthropic-ratelimit-part-{key[9:21].upper()}": "9",  # eight in a row, folded the other way
            # every byte escaped, which no escaper that keeps unreserved characters writes
            "anthropic-ratelimit-escaped-" + "".join(f"%{b:02X}" for b in key.encode()): "10",
        }

    _respond(monkeypatch, _Named(json.dumps(_ok("done"))))
    backend = Anthropic(model="claude-haiku-5-5", retries=0)
    asyncio.run(backend([Message("user", "hi", name="j")]))
    limits = backend.calls[-1]["limits"]
    assert limits["request-id"] == "req_named" and len(limits) == 5, limits
    written = json.dumps(backend.calls).lower()
    assert not any(w.lower() in written for w in _windows(key)), limits
    assert key[9:17].lower() not in urllib.parse.unquote(written), limits
    assert "anthropic-ratelimit-[key]" in limits and "anthropic-ratelimit-folded-[key]" in limits
    assert "anthropic-ratelimit-escaped-[key]" in limits, limits


def test_an_account_name_holding_a_credential_stem_is_refused_before_the_tool_and_any_envelope(
    monkeypatch, tmp_path
):
    with pytest.raises(ValueError, match="never holding 'sk-ant'") as e:
        Anthropic(model="claude-haiku-5-5", account="my-SK-ANT-account")
    assert "my-SK-ANT" not in str(e.value)
    tool = tmp_path / "security"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$0.argv"\nprintf "sk-ant-item-0123456789\\n"\n')
    tool.chmod(0o755)
    monkeypatch.setattr(mod, "_SECURITY", str(tool))
    with pytest.raises(AnthropicError, match="never holding") as e:
        mod.api_key("sk-ant-api03-0123456789")
    assert not (tmp_path / "security.argv").exists() and "api03" not in str(e.value)


def test_a_key_that_is_not_one_line_of_printable_ascii_is_refused_before_any_request(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: _raise(AssertionError("sent")))
    for bad in ("sk-ant-two\nlines", "sk-ant-with space", "sk-ant-ключ", " sk-ant-padded"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", bad)
        backend = Anthropic(model="claude-haiku-5-5")
        backend._tail = "before"
        with pytest.raises(AnthropicError, match="printable ASCII") as e:
            asyncio.run(backend([Message("user", "hi", name="j")]))
        (failed,) = backend.calls
        assert failed["attempts"] == 0 and failed["error"].startswith("ANTHROPIC_API_KEY in the environment")
        assert backend._tail == "before"
        assert bad[:8] not in str(e.value) and bad[:8] not in json.dumps(failed), bad
    # a keychain item of two lines is refused the same way, naming the account and not the value
    tool = tmp_path / "security"
    tool.write_text('#!/bin/sh\nprintf "sk-ant-item-0123456789\\nsecond-line\\n"\n')
    tool.chmod(0o755)
    monkeypatch.setattr(mod, "_SECURITY", str(tool))
    with pytest.raises(AnthropicError, match="item of account 'anthropic_api_key_a' is not one line") as e:
        mod.api_key("anthropic_api_key_a")
    assert "sk-ant-item" not in str(e.value) and "second" not in str(e.value)


def test_a_refusal_before_dispatch_keeps_the_tail_and_a_cancellation_while_posting_moves_it(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", _answers(_ok("a")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-made-up-key-0123456789")
    backend = Anthropic(model="claude-haiku-5-5")
    asyncio.run(backend([Message("user", "[j1] go", name="j1")]))
    assert backend._tail == "j1"
    longer = [Message("user", "[j1] go", name="j1"), Message("user", "[j2] on", name="j2")]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(AnthropicError, match="not in the environment"):
        asyncio.run(backend(longer))
    assert backend._tail == "j1" and backend.calls[-1]["attempts"] == 0  # nothing went out
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-made-up-key-0123456789")

    def cancelled(body, timeout, key, heard=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "_post", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend(longer))
    cut = backend.calls[-1]
    assert backend._tail == "j2"  # the request was out: the provider may hold the prefix
    assert cut["error"] == "cancelled" and cut["attempts"] == 1 and cut["attempt_usage"] == [{}]


def test_retry_after_as_an_http_date_is_read_against_now_and_every_wait_is_within_the_bound():
    import datetime
    import email.utils

    now = datetime.datetime.now(datetime.UTC)
    ahead = email.utils.format_datetime(now + datetime.timedelta(seconds=35), usegmt=True)
    behind = email.utils.format_datetime(now - datetime.timedelta(seconds=60), usegmt=True)
    assert 33.0 <= mod._retry_after({"retry-after": ahead}) <= 35.0
    assert mod._retry_after({"retry-after": behind}) == 0.0
    assert mod._retry_after({"retry-after": "soon"}) is None and mod._retry_after({}) is None
    assert mod._wait(0, mod._retry_after({"retry-after": ahead})) <= 35.0
    assert mod._wait(30, None) == mod._RETRY_AFTER_MAX and mod._wait(5, None) == 64.0


def test_a_transport_error_echoing_the_key_is_redacted_like_a_body(monkeypatch):
    import urllib.error

    key = "sk-ant-made-up-key-0123456789abcdef"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    for raised in (urllib.error.URLError(f"resolver says {key}"), ConnectionResetError(f"reset after {key}")):
        _respond(monkeypatch, raised)
        backend = Anthropic(model="claude-haiku-5-5", retries=0)
        with pytest.raises(AnthropicError, match="request failed") as e:
            asyncio.run(backend([Message("user", "hi", name="j")]))
        written = str(e.value) + json.dumps(backend.calls)
        assert "[key]" in str(e.value) and not any(w in written for w in _windows(key)), raised


def test_a_keychain_value_is_read_as_stored_and_refused_outside_the_header_form_naming_no_byte(
    monkeypatch, tmp_path
):
    tool = tmp_path / "security"
    monkeypatch.setattr(mod, "_SECURITY", str(tool))
    cases = {
        "padded": b'printf " sk-ant-item-0123456789\\n"',
        "carriage": b'printf "sk-ant-item-0123456789\\r\\n"',
        "bytes": b'printf "sk-ant-item-\\377\\376-0123456789\\n"',
        "long": b'printf "%0.s" ; head -c 1025 /dev/zero | tr "\\0" "k"; printf "\\n"',
    }
    for label, line in cases.items():
        tool.write_bytes(b"#!/bin/sh\n" + line + b"\n")
        tool.chmod(0o755)
        with pytest.raises(
            AnthropicError, match="item of account 'anthropic_api_key_a' is not one line"
        ) as e:
            mod.api_key("anthropic_api_key_a")
        assert (
            "0123456789" not in str(e.value) and "\\x" not in str(e.value) and "position" not in str(e.value)
        ), label
    tool.write_bytes(b'#!/bin/sh\nprintf "sk-ant-item-0123456789\\n"\n')
    assert mod.api_key("anthropic_api_key_a") == "sk-ant-item-0123456789"
    assert mod._KEY.fullmatch("k" * 1024) and not mod._KEY.fullmatch("k" * 1025)


def test_a_cancellation_before_the_worker_took_the_request_bills_nothing_and_one_in_a_backoff_counts_it(
    monkeypatch,
):
    class _Queued(_NoSleep):
        @staticmethod
        async def to_thread(fn, *args):
            raise asyncio.CancelledError()  # cancelled behind a busy executor: the worker never ran

    monkeypatch.setattr(mod, "asyncio", _Queued)
    monkeypatch.setattr(mod, "_post", _answers(_ok("never")))
    backend = Anthropic(model="claude-haiku-5-5")
    backend._tail = "before"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (cut,) = backend.calls
    assert backend._tail == "before" and cut["attempts"] == 0 and cut["attempt_usage"] == []
    assert cut["total_cost_usd"] == 0.0 and cut["cost_complete"]

    class _Backoff(_NoSleep):
        @staticmethod
        async def sleep(_seconds):
            raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "asyncio", _Backoff)
    busy = AnthropicError("HTTP 429: busy", status=429, headers={"request-id": "r429"})
    monkeypatch.setattr(mod, "_post", _answers(busy, _ok("never")))
    backend = Anthropic(model="claude-haiku-5-5")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (cut,) = backend.calls
    assert backend._tail == "j" and cut["attempts"] == 1 and cut["attempt_usage"] == []
    assert cut["limits"] == {"request-id": "r429"}


def test_a_200_in_another_shape_is_a_failed_attempt_on_the_envelope_not_an_exception(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    shapes = (
        {"content": [{"type": "text", "text": "x"}], "usage": [1], "stop_reason": "end_turn"},
        {"content": "x", "usage": {}, "stop_reason": "end_turn"},
        {"content": [{"type": "text", "text": "x"}, 3], "usage": {}, "stop_reason": "end_turn"},
        {"content": [{"type": "text", "text": "x"}], "usage": {}, "stop_reason": ["end_turn"]},
    )
    for shape in shapes:
        _respond(monkeypatch, _Body(json.dumps(shape)))
        backend = Anthropic(model="claude-haiku-5-5", retries=0)
        with pytest.raises(AnthropicError, match="unreadable response body"):
            asyncio.run(backend([Message("user", "hi", name="j")]))
        (failed,) = backend.calls
        assert failed["attempts"] == 1 and failed["attempt_usage"] == [{}] and not failed["cost_complete"]
        assert failed["limits"] == {"request-id": "r"}
    # a count table whose parts are not tables reads as unknown, never as an error
    odd = {**_usage(), "cache_creation": [5], "output_tokens_details": "many"}
    monkeypatch.setattr(mod, "_post", _answers(_ok("x", usage=odd)))
    backend = Anthropic(model="claude-haiku-5-5")
    asyncio.run(backend([Message("user", "hi", name="j")]))
    assert (
        backend.calls[-1]["total_cost_usd"] is None
        and backend.calls[-1]["usage"]["reasoning_output_tokens"] is None
    )


def test_an_account_changed_after_construction_to_a_credentials_shape_is_refused_and_recorded_nowhere(
    monkeypatch,
):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: _raise(AssertionError("sent")))
    backend = Anthropic(model="claude-haiku-5-5", account="a")
    backend.account = "sk-ant-api03-0123456789"
    with pytest.raises(AnthropicError, match="never holding"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert "account" not in failed and "api03" not in json.dumps(backend.calls)
    backend.account = 7
    with pytest.raises(AnthropicError, match="never holding"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    assert "account" not in backend.calls[-1]


def test_a_retry_after_that_is_not_a_finite_number_is_read_as_absent():
    for v in ("nan", "inf", "-inf", "NaN"):
        assert mod._retry_after({"retry-after": v}) is None, v
    assert mod._retry_after({"retry-after": "7.5"}) == 7.5 and mod._wait(0, None) == 2.0


def test_an_echo_of_the_key_in_an_encoding_a_reader_decodes_is_redacted_from_the_error(monkeypatch):
    import base64
    import urllib.parse

    key = "sk-ant-made-up-key-0123456789abcdef+/=?&"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key.replace("+/=?&", "zzzzz"))
    key = key.replace("+/=?&", "zzzzz")
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    header = f"x-api-key: {key}".encode()
    echoes = {
        "percent": urllib.parse.quote(key, safe=""),
        "every byte percent-escaped": "".join(f"%{b:02X}" for b in key.encode()),
        "every byte percent-escaped, lower": "".join(f"%{b:02x}" for b in key.encode()),
        "hex": key.encode().hex(),
        "HEX": key.encode().hex().upper(),
        "base64 at each alignment": " ".join(
            base64.b64encode(p + key.encode()).decode() for p in (b"", b"a", b"ab")
        ),
        "url-safe base64 of the header line": base64.urlsafe_b64encode(header).decode(),
    }
    # the key's own characters in each form, sixteen in a row: what must not survive, whatever surrounds it
    raw = key.encode()
    cores = [base64.b64encode(raw[i:][: len(raw[i:]) // 3 * 3]).decode() for i in range(3)]
    secret = [
        key,
        raw.hex(),
        raw.hex().upper(),
        "".join(f"%{b:02X}" for b in raw),
        "".join(f"%{b:02x}" for b in raw),
        *cores,
        *(c.replace("+", "-").replace("/", "_") for c in cores),
    ]
    for label, echo in echoes.items():
        _respond(monkeypatch, _http_error(400, json.dumps({"error": {"message": f"you sent {echo}"}})))
        backend = Anthropic(model="claude-haiku-5-5", retries=0)
        with pytest.raises(AnthropicError, match="HTTP 400") as e:
            asyncio.run(backend([Message("user", "hi", name="j")]))
        written = str(e.value) + json.dumps(backend.calls)
        assert "[key]" in str(e.value), label
        for form in secret:
            for i in range(len(form) - 15):
                assert form[i : i + 16] not in written, (label, form[i : i + 16])
    # the redaction leaves text that holds none of it alone, a short key included
    assert mod._redact("prompt is too long: 210000 tokens > 200000 maximum", key) == (
        "prompt is too long: 210000 tokens > 200000 maximum"
    )
    assert mod._redact("x", "short") == "x" and mod._redact("abcdefghij", "abcdefghij") == "[key]"


def test_every_field_a_success_carries_is_scrubbed_of_the_key_and_the_envelope_says_so(monkeypatch):
    key = "sk-ant-made-up-key-0123456789abcdef"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(mod, "asyncio", _NoSleep)

    class _Echo(_Body):
        headers = {"request-id": "req_1", "anthropic-ratelimit-tokens-remaining": f"9 {key}"}

    body = {
        "id": "msg_1",
        "model": "claude-haiku-5-5",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t", "name": "run", "input": {key: f"echo {key[3:30]}"}}],
        "usage": _usage() | {"model": None},
        "extension": {"note": f"saw {key}"},
    }
    del body["usage"]["model"]
    _respond(monkeypatch, _Echo(json.dumps(body)))
    backend = Anthropic(model="claude-haiku-5-5", retries=0)
    reply = asyncio.run(backend([Message("user", "hi", name="j")]))
    written = str(reply) + json.dumps(backend.calls)
    assert not any(w in written for w in _windows(key)), written
    (envelope,) = backend.calls
    assert envelope["key_redacted"] is True and envelope["limits"]["request-id"] == "req_1"
    assert (
        "[key]" in envelope["extension"]["note"]
        and "[key]" in envelope["limits"]["anthropic-ratelimit-tokens-remaining"]
    )
    # a clean answer carries no such mark
    _respond(monkeypatch, _Body(json.dumps(_ok("fine"))))
    asyncio.run(backend([Message("user", "hi", name="j")]))
    assert "key_redacted" not in backend.calls[-1]


def test_a_cancellation_closes_the_record_so_a_queued_request_never_leaves_and_an_overtaken_answer_is_billed(
    monkeypatch,
):
    posted = []

    def post(body, timeout, key, heard=None):
        posted.append(body)
        return _ok("answer"), {"request-id": "r_late"}

    monkeypatch.setattr(mod, "_post", post)
    waiting = []

    class _Queued(_NoSleep):
        @staticmethod
        async def to_thread(fn, *args):
            waiting.append(fn)  # the worker has not started it yet
            raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "asyncio", _Queued)
    backend = Anthropic(model="claude-haiku-5-5")
    backend._tail = "before"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    assert waiting[0]() is None and posted == []  # the worker starts after the cancel: nothing goes out
    assert backend.calls[-1]["attempts"] == 0 and backend._tail == "before"

    class _Overtaken(_NoSleep):
        @staticmethod
        async def to_thread(fn, *args):
            fn()  # the worker answered; the cancellation lands before the loop takes the answer in
            raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "asyncio", _Overtaken)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    cut = backend.calls[-1]
    assert cut["attempts"] == 1 and backend._tail == "j" and cut["usage_complete"] and cut["cost_complete"]
    assert cut["total_cost_usd"] > 0 and cut["limits"] == {"request-id": "r_late"}


def test_a_200_without_content_or_with_unreadable_values_is_a_failed_attempt_and_a_huge_count_is_unknown(
    monkeypatch,
):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    bodies = (
        json.dumps({"usage": {}, "stop_reason": "end_turn"}),
        json.dumps({"content": None, "usage": {}, "stop_reason": "end_turn"}),
        json.dumps({"content": [{"type": "text", "text": 7}], "usage": {}, "stop_reason": "end_turn"}),
        json.dumps({"content": [{"type": "text"}], "usage": {}, "stop_reason": "end_turn"}),
        '{"content": [], "usage": {"input_tokens": ' + "9" * 5000 + "}}",
        "[" * 100_000 + "]" * 100_000,
    )
    for raw in bodies:
        _respond(monkeypatch, _Body(raw))
        backend = Anthropic(model="claude-haiku-5-5", retries=0)
        with pytest.raises(AnthropicError, match="unreadable response body"):
            asyncio.run(backend([Message("user", "hi", name="j")]))
        (failed,) = backend.calls
        assert failed["attempts"] == 1 and failed["attempt_usage"] == [{}] and not failed["cost_complete"], (
            raw[:60]
        )
    huge = _usage() | {"input_tokens": 10**400}
    monkeypatch.setattr(mod, "_post", _answers(_ok("x", usage=huge)))
    backend = Anthropic(model="claude-haiku-5-5")
    asyncio.run(backend([Message("user", "hi", name="j")]))
    (envelope,) = backend.calls
    assert envelope["usage"]["input_tokens"] is None and envelope["total_cost_usd"] is None
    assert not envelope["usage_complete"] and not envelope["cost_complete"]


def test_any_other_error_past_the_preflight_lands_a_failed_envelope_naming_only_its_class(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    odd = {"id": "m", "model": "claude-haiku-5-5", "content": [], "stop_reason": "end_turn", "usage": [1]}
    monkeypatch.setattr(mod, "_post", _answers(odd))
    backend = Anthropic(model="claude-haiku-5-5")
    with pytest.raises(AnthropicError, match=r"^the response could not be read: TypeError$"):
        asyncio.run(backend([Message("user", "hi", name="j")]))
    (failed,) = backend.calls
    assert failed["attempts"] == 1 and failed["error"] == "the response could not be read: TypeError"
    assert backend._tail == "j"


def test_a_key_under_eight_characters_is_refused_and_eight_in_a_row_are_redacted_in_base64_at_any_offset(
    monkeypatch,
):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    sent = []
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: sent.append(req))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "e" * 7)
    backend = Anthropic(model="claude-haiku-5-5")
    with pytest.raises(AnthropicError, match="8 to 1024 characters") as e:
        asyncio.run(backend([Message("user", "hi", name="j")]))
    assert "eeeeeee" not in str(e.value) and sent == [] and backend.calls[-1]["attempts"] == 0
    # any eight of the key's characters in a row, base64-encoded at each of the three byte offsets a longer
    # text can put them at, standard and URL-safe, are redacted; so is a short key whole
    for key in ("sk-ant-made-up-key-0123456789", "k3y-n1ne!"):
        for start in range(len(key) - 7):
            piece = key[start : start + 8].encode()
            for offset in range(3):
                for encode in (base64.b64encode, base64.urlsafe_b64encode):
                    echo = encode(b"P" * offset + piece + b"Q" * 3).decode()
                    assert "[key]" in mod._redact(echo, key), (key, start, offset)


def test_a_usage_reporting_a_zero_prompt_anchors_nothing_and_bills_as_unknown(monkeypatch):
    monkeypatch.setattr(mod, "asyncio", _NoSleep)
    monkeypatch.setattr(mod, "_post", _answers(_ok("fine", usage=_usage(inp=0, out=0))))
    backend = Anthropic(model="claude-haiku-5-5")
    reply = asyncio.run(backend([Message("user", "hi", name="j")]))
    assert reply == "fine" and reply.prompt_tokens is None  # a non-positive count anchors nothing
    call = backend.calls[-1]
    assert call["usage"]["prompt_tokens"] is None and call["usage_complete"] is False
    assert call["total_cost_usd"] is None and call["cost_complete"] is False


def test_a_cancellation_while_the_body_is_coming_keeps_the_headers_already_read(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class _Slow(_Body):
        headers = {"request-id": "req_slow", "anthropic-ratelimit-requests-remaining": "7"}

        def read(self):
            entered.set()
            release.wait(5)
            return super().read()

    _respond(monkeypatch, _Slow(json.dumps(_ok("late"))))
    backend = Anthropic(model="claude-haiku-5-5", retries=0)

    async def go():
        task = asyncio.create_task(backend([Message("user", "hi", name="j")]))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(go())
    (failed,) = backend.calls
    assert failed["error"] == "cancelled" and failed["attempts"] == 1
    assert failed["limits"] == {"request-id": "req_slow", "anthropic-ratelimit-requests-remaining": "7"}
