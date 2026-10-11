"""The Messages endpoint as a chat-completion backend (ADR-0028): the view in as the provider's own
shape, text out, the prompt cache placed by the backend.

The head rides as `system`, one text block with a breakpoint. Every other message is one text block
under `user` or `assistant`, a notification as `user`. Two more breakpoints: the previous call's tail,
while the view still holds it (the provider looks back twenty blocks for an entry, and a turn with many
results adds more than that), and the current tail. One TTL per backend, on every breakpoint.

The key is the account's keychain item (service `ANTHROPIC_API_KEY`, account the pool's name), or with
no account `ANTHROPIC_API_KEY` in the environment. It rides the request header only: never an envelope,
an error or a log, and everything the provider sends back is scrubbed of it. Cost is computed from `PRICES`,
dated below, per request at the request's tier; a served model the table lacks has no cost.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import email.utils
import http.client
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from asyncio import CancelledError  # by name: tests stand in for the module's `asyncio`
from collections.abc import Callable
from dataclasses import dataclass, field

from lion1.actor import Message, Reply

from .markup import normalize, tool_calls_text

__all__ = (
    "URL",
    "PRICES",
    "PRICES_DATED",
    "AnthropicError",
    "api_key",
    "priced",
    "Anthropic",
)

URL = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"
SERVICE = "ANTHROPIC_API_KEY"  # the keychain item's service; its account is the pool's name
TTLS = ("5m", "1h")
_SECURITY = "/usr/bin/security"
# the pool's label form (ADR-0024/C1): the name rides the tool's argv and every envelope, so one outside
# the form, or holding a credential's stem in any case, is refused by rule and never echoed
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_LABEL = "the account's name is letters, digits, '.', '_' and '-', at most 64 of them, never holding 'sk-ant'"
# what a header value takes: one line of printable ASCII, no space, and a length no key form approaches;
# at least the eight characters `_redact` matches by, so a shorter value cannot turn ordinary text into
# a match and every encoding of the whole key holds a window
_KEY = re.compile(r"[\x21-\x7e]{8,1024}")
_KEY_FORM = "one line of printable ASCII, 8 to 1024 characters, which the request header takes"
# the start every key of a provider's form shares, printed in its documentation: `sk-ant-api03-` here,
# `sk-or-v1-` for the router. It names the form, never a key, so a window wholly inside it is no match
_PUBLIC = re.compile(r"sk-(?:ant-[a-z]+[0-9]+|or-v[0-9]+)-")
_EMPTY = "[empty]"  # a text block must hold text; an empty entry is sent as this
_STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}
_RETRY_AFTER_MAX = 120.0  # seconds; the longest one wait: a longer `Retry-After` or backoff waits this many
_COUNT_MAX = 10**9  # tokens; a count past this is no attempt's, and reads as unknown

PRICES_DATED = "2026-10-09"
# dollars per million tokens: base input, 5m write, 1h write, cache read, output; keyed by the served
# model, as the response names it. A tier is (prompt tokens the band ends at, prices); None ends nowhere
PRICES: dict[str, tuple[tuple[int | None, tuple[float, float, float, float, float]], ...]] = {
    "claude-haiku-5-5": ((100_000, (0.10, 0.125, 0.20, 0.01, 0.50)), (None, (0.50, 0.625, 1.00, 0.05, 2.50))),
    "claude-haiku-4-5": ((None, (1.00, 1.25, 2.00, 0.10, 5.00)),),
    "claude-sonnet-5-5": ((None, (2.00, 2.50, 4.00, 0.10, 10.00)),),
    "claude-sonnet-5": ((None, (2.00, 2.50, 4.00, 0.20, 10.00)),),
    "claude-opus-5-5": ((None, (4.00, 5.00, 8.00, 0.20, 20.00)),),
    "claude-opus-5": ((None, (5.00, 6.25, 10.00, 0.50, 25.00)),),
}


class AnthropicError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
        headers: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.headers = headers or {}  # the refusing response's, so its limits and request id are kept


def _named(name: object) -> bool:
    """Whether the name is in the pool's label form and holds no credential stem."""
    return isinstance(name, str) and bool(_NAME.fullmatch(name)) and "sk-ant" not in name.lower()


def _account(name: object) -> None:
    if not _named(name):
        raise AnthropicError(_LABEL)


def api_key(account: str | None) -> str:
    """The key: with no account, `ANTHROPIC_API_KEY` from the environment; with one, its keychain item.
    The name is checked against the pool's label form before it rides the tool's arguments, and the key
    against the header's form before it rides a request: a refusal names the source, never the value."""
    if account is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise AnthropicError("ANTHROPIC_API_KEY: not in the environment, and no account was named")
        source = "ANTHROPIC_API_KEY in the environment"
    else:
        _account(account)
        try:
            out = subprocess.run(
                [_SECURITY, "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            out = None
        if out is None or out.returncode or not out.stdout.strip():
            raise AnthropicError(
                f"account {account!r} has no keychain item under {SERVICE}; `lion account keys` writes it"
            )
        # the tool prints the value and one newline; what came before it is the value as stored, and a
        # byte outside ASCII becomes a character the form below refuses, so no byte of it is ever named
        key = out.stdout.removesuffix(b"\n").decode("ascii", errors="replace")
        source = f"the keychain item of account {account!r}"
    if not _KEY.fullmatch(key):
        raise AnthropicError(f"{source} is not {_KEY_FORM}")
    return key


def _windows(key: str) -> dict[int, frozenset[str]]:
    """Eight of the key's characters in a row, as each form a reader decodes prints them, keyed by the
    width that takes: the key as sent and percent-escaped where an escaper must, eight characters; hex
    in either case, sixteen; every byte percent-escaped, in either case, twenty-four, since a key of
    unreserved characters is its own escaped form and only an escaper that escapes everything changes
    it; base64, standard and URL-safe, from each of the three byte alignments a longer encoded text
    can start it at, eight: two full groups, six of the key's bytes, which any eight of them in a row
    hold whatever the alignment, and full groups only, so no neighbouring byte decides a character.
    A window wholly inside the key's public start (`_PUBLIC`) is left out, so an answer that names the
    form is not rewritten; every window holding a character past it stays, told apart by its place in
    the form and never by its text, so a run of the secret part spelling the same characters matches."""
    whole = _forms(key)
    public = _PUBLIC.match(key)
    # each form of the public start begins the same form of the key, every form here encoding byte by
    # byte and base64 in full groups, so a window ending past that form's length holds a character past
    # the start
    head = _forms(public.group()) if public else {w: [""] * len(fs) for w, fs in whole.items()}
    return {
        w: frozenset(
            f[j : j + w]
            for f, h in zip(fs, head[w], strict=True)
            for j in range(max(len(h) - w + 1, 0), len(f) - w + 1)
        )
        for w, fs in whole.items()
    }


def _forms(text: str) -> dict[int, list[str]]:
    raw = text.encode()
    forms: dict[int, list[str]] = {8: [text, urllib.parse.quote(text, safe="")], 16: [], 24: []}
    forms[16] += [raw.hex(), raw.hex().upper()]
    escaped = "".join(f"%{b:02X}" for b in raw)
    forms[24] += [escaped, escaped.lower()]
    for i in range(3):
        part = raw[i:]
        core = base64.b64encode(part[: len(part) // 3 * 3]).decode()
        forms[8] += [core, core.replace("+", "-").replace("/", "_")]
    return forms


def _redact(text: str, key: str, windows: dict[int, frozenset[str]] | None = None) -> str:
    """The provider's text with the key, and eight of its characters in a row in any form `_windows`
    names, replaced: an echo of the request's credential that a reader could decode reaches no error,
    envelope or reply (ADR-0028/C3). An echo built to evade, split or re-encoded, is not recognised:
    the provider holds the key already, and what this guards is an accidental copy into the records."""
    text = text.replace(key, "[key]")
    windows = _windows(key) if windows is None else windows
    spans = sorted(
        (j, j + w)
        for w, ws in windows.items()
        if ws
        for j in range(len(text) - w + 1)
        if text[j : j + w] in ws
    )
    if not spans:
        return text
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, at = [], 0
    for start, end in merged:
        out += [text[at:start], "[key]"]
        at = end
    return "".join(out) + text[at:]


def _scrubbed(value: object, key: str, windows: dict[int, frozenset[str]]) -> tuple[object, bool]:
    """A parsed body with every string in it, names included, redacted of the key; and whether any was."""
    if isinstance(value, str):
        clean = _redact(value, key, windows)
        return clean, clean != value
    if isinstance(value, list):
        items = [_scrubbed(v, key, windows) for v in value]
        return [v for v, _ in items], any(c for _, c in items)
    if isinstance(value, dict):
        pairs = [(_scrubbed(k, key, windows), _scrubbed(v, key, windows)) for k, v in value.items()]
        return {k: v for (k, _), (v, _) in pairs}, any(ck or cv for (_, ck), (_, cv) in pairs)
    return value, False


def priced(model: str) -> bool:
    """Whether `PRICES` prices the model, so a budgeted account can refuse one it cannot (ADR-0028/C4)."""
    return model in PRICES


@dataclass
class Anthropic:
    model: str
    timeout: float = 300.0
    # a turn is a whole program, sized as the router's is (ADR-0009/D1)
    max_tokens: int = 32768
    temperature: float | None = None
    stop: list[str] | None = None  # sequences that end the completion; the text before them is the answer
    # the model's thinking effort (low, medium, high), fixed for the run; None sends no thinking setting
    # and the model decides. The current models take adaptive thinking under an effort; a budget in
    # tokens is refused (measured 2026-10-09: `thinking.type.enabled` is a 400 on all three)
    effort: str | None = None
    ttl: str = "5m"  # the cache life on every breakpoint: 5m, or 1h for calls spaced past five minutes
    retries: int = 4  # on a transient failure (429, 5xx, a transport failure, an empty answer), with backoff
    # the pool's account whose keychain item holds the key; None reads the environment
    account: str | None = None
    calls: list[dict] = field(default_factory=list)  # response envelope per call, minus the text
    _tail: str | None = field(default=None, init=False, repr=False)  # the previous call's tail, by entry name

    def __post_init__(self) -> None:
        if self.ttl not in TTLS:
            raise ValueError(f"ttl is one of {', '.join(TTLS)}, not {self.ttl!r}")
        if self.account is not None:
            try:
                _account(self.account)  # before any envelope could carry the name
            except AnthropicError as e:
                refused = str(e)
            else:
                refused = None
            if refused is not None:
                raise ValueError(refused)  # past the handler, with no context (ADR-0009 S32)

    def body(self, messages: list[Message]) -> dict:
        """The request for the view: the head as `system` with a breakpoint, the rest as text blocks, the
        previous call's tail pinned when the view still holds it, the tail marked. Three breakpoints at
        most, one TTL on all of them (ADR-0028/C1)."""
        head = messages[0] if messages and messages[0].role == "system" else None
        rest = messages[1:] if head is not None else list(messages)
        if not rest:
            raise AnthropicError("the view has no message after the head; the endpoint takes at least one")
        mark = {"type": "ephemeral", "ttl": self.ttl}
        out: list[dict] = []
        pinned = False
        for i, m in enumerate(rest):
            block: dict = {"type": "text", "text": m.content if m.content.strip() else _EMPTY}
            last = i == len(rest) - 1
            if last or (not pinned and not m.folded and m.name is not None and m.name == self._tail):
                block["cache_control"] = dict(mark)
                pinned = pinned or not last
            # only the head is system; a notification mid-conversation goes as user (ADR-0009/C1)
            out.append({"role": "assistant" if m.role == "assistant" else "user", "content": [block]})
        body: dict = {"model": self.model, "max_tokens": self.max_tokens, "messages": out}
        if head is not None:
            body["system"] = [{"type": "text", "text": head.content, "cache_control": dict(mark)}]
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.stop:
            body["stop_sequences"] = self.stop
        if self.effort is not None:
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": self.effort}
        return body

    async def __call__(self, messages: list[Message]) -> str:
        t0 = time.monotonic()
        try:
            body = self.body(messages)
            key = api_key(self.account)
            payload = json.dumps(body).encode()
        except Exception as e:
            # refused here, nothing sent: the envelope says so, with a known zero (ADR-0028/C2), and the
            # tail stays where the last request left it
            unsent = (
                str(e)
                if isinstance(e, AnthropicError)
                else f"the request could not be built: {type(e).__name__}"
            )
            self._bill_failure([], unsent, 0, t0, {})
        else:
            unsent = None
        if unsent is not None:
            raise AnthropicError(unsent)  # past the handler, with no context (ADR-0009 S32)
        tail = messages[-1].name
        # every response's usage with its served model, so a retried attempt's bill is not lost
        spent: list[dict] = []
        headers: dict = {}
        # the worker's record of the requests, under one lock: `sent` counts those it took up, refusals
        # included, and `landed` holds the last one's answer or error once it has one. A cancellation
        # closes the record and reads it in one step, so no request leaves after the count is taken
        lock = threading.Lock()
        closed = False
        sent = 0
        landed: tuple[dict, dict] | AnthropicError | None = None
        handled = 0  # the requests whose outcome the loop took in

        heard: dict = {}  # the headers of the request out, once the worker has read them

        def hear(h: dict) -> None:
            nonlocal heard
            with lock:
                heard = h

        def dispatch() -> tuple[dict, dict] | None:
            nonlocal sent, landed, heard
            with lock:
                if closed:
                    return None  # the call ended while this waited for a worker: nothing goes out
                landed = None
                heard = {}
                sent += 1
            try:
                answer = _post(payload, self.timeout, key, heard=hear)
            except AnthropicError as e:
                with lock:
                    landed = e
                raise
            with lock:
                landed = answer
            return answer

        def overtaken() -> int:
            # the call ends while a request is out: the worker's record decides what it billed, and the
            # provider may hold the prefix. The requests sent, read as the record closes
            nonlocal closed, headers
            with lock:
                closed = True
                count, outcome, seen = sent, landed, heard
            if count == handled:
                return count
            self._tail = tail
            if isinstance(outcome, tuple):
                spent.append(self._spend(outcome[0]))  # answered, then overtaken: its bill is known
                headers = outcome[1]
            elif isinstance(outcome, AnthropicError) and not str(outcome).startswith("request failed"):
                headers = outcome.headers or headers  # refused by the provider: nothing billed
            else:
                spent.append({})  # still out, or lost in transport: billed or not is unknown
                headers = seen or headers  # its response's, when the worker had read them
            return count

        try:
            for attempt in range(self.retries + 1):
                try:
                    data, headers = await asyncio.to_thread(dispatch)
                except AnthropicError as e:
                    handled = sent
                    headers = e.headers or headers
                    self._tail = tail  # sent: the provider may hold the prefix whatever came back
                    if str(e).startswith("request failed"):
                        # it went out and no readable answer came back: billed or not is unknown
                        spent.append({})
                    if attempt < self.retries and _transient(str(e)):
                        await asyncio.sleep(_wait(attempt, e.retry_after))
                        continue
                    self._bill_failure(spent, str(e), attempt + 1, t0, headers)
                    raise
                handled = sent
                self._tail = tail
                spent.append(self._spend(data))
                content = data.get("content") or []
                texts = [b.get("text") or "" for b in content if b.get("type") == "text"]
                uses = [b for b in content if b.get("type") == "tool_use"]
                text = "\n".join(texts)
                stop = data.get("stop_reason")
                if not text.strip() and uses:
                    # no tools were offered, so a tool_use block is the model's turn in its native shape:
                    # handed back as the text it wrote, as the router does (ADR-0004/C4)
                    text = tool_calls_text(
                        [
                            {
                                "id": u.get("id"),
                                "function": {"name": u.get("name"), "arguments": u.get("input")},
                            }
                            for u in uses
                        ]
                    )
                if text or stop == "max_tokens":
                    break
                # a finished answer with nothing in it: sampled again, then handed over blank
                if attempt < self.retries:
                    await asyncio.sleep(1.0)
                    continue
                break
            envelope = {k: v for k, v in data.items() if k not in ("content", "usage")}
            envelope["finish_reason"] = _STOP.get(stop, stop)
            envelope["native_finish_reason"] = stop
            if uses:
                envelope["tool_calls"] = uses
            if not text.strip():
                # nothing readable came back: keep the content, so the turn can be diagnosed from the
                # record, less its thinking blocks, which are not kept (D1)
                envelope["message"] = [
                    b for b in content if b.get("type") not in ("thinking", "redacted_thinking")
                ]
            envelope["attempts"] = attempt + 1
            envelope["duration_api_ms"] = int((time.monotonic() - t0) * 1000)  # retries and backoff included
            envelope["usage"] = _usage(spent)
            envelope["usage_complete"] = _counted(spent)
            envelope["total_cost_usd"] = _cost(spent)
            envelope["cost_complete"] = _complete(spent)
            envelope["tier"] = _tier(spent[-1])
            envelope["limits"] = _limits(headers)
            if self.account is not None:
                envelope["account"] = self.account
            if len(spent) > 1:
                envelope["attempt_usage"] = spent
            refused = None
            if text.strip() and texts and any(t.strip() for t in texts) and uses:
                # the model's turn in both channels: read by halves, either could run without the other,
                # so none of it runs and the model is told (ADR-0004/C4); the envelope keeps the calls
                refused = (
                    "the turn came back in two channels, as text and as a native tool call, and nothing in "
                    "it was executed: write the whole turn as text, the tags included; this conversation "
                    "has no tool-call API"
                )
            reply = Reply(normalize(text), prompt_tokens=_prompt(spent[-1]), refused=refused)
        except CancelledError:
            self._bill_failure(spent, "cancelled", overtaken(), t0, headers)
            raise
        except AnthropicError:
            raise
        except Exception as e:
            # anything else past the preflight still lands a failed envelope (ADR-0009/C5); the error names
            # the class only, since its text may carry what the provider sent
            error = f"the response could not be read: {type(e).__name__}"
            self._bill_failure(spent, error, overtaken(), t0, headers)
        else:
            self.calls.append(envelope)
            return reply
        raise AnthropicError(error)  # past the handler, with no context (ADR-0009 S32)

    def _spend(self, data: dict) -> dict:
        # the request's TTL beside the usage: every breakpoint carried it, so a write the response does not
        # split is still priced at it (ADR-0028/C2)
        return {**(data.get("usage") or {}), "model": data.get("model"), "ttl": self.ttl}

    def _bill_failure(self, spent: list[dict], error: str, attempts: int, t0: float, headers: dict) -> None:
        """Every call that ends in an error lands on `calls` as a failed envelope carrying what was
        observed (ADR-0009/C5): the attempts the provider billed, or may have billed; none when it was
        refused before any, here or by the provider, whose cost is a complete zero. The last response's
        limits and request id ride it, a refusal's included."""
        self.calls.append(
            {
                "error": error,
                "attempts": attempts,
                "duration_api_ms": int((time.monotonic() - t0) * 1000),
                "usage": _usage(spent),
                "usage_complete": _counted(spent),
                "total_cost_usd": _cost(spent) if spent else 0.0,
                "cost_complete": _complete(spent),
                "limits": _limits(headers),
                "attempt_usage": spent,
                # the name as validated: one changed after construction to a credential's shape was
                # refused before any request and is recorded nowhere
                **({"account": self.account} if _named(self.account) else {}),
            }
        )


COUNTS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def _n(u: dict, key: str) -> int | None:
    """A count as the provider reported it; None when it is absent, not a count or past any attempt's
    (`_COUNT_MAX`): an unknown is never read as zero."""
    v = u.get(key) if isinstance(u, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= _COUNT_MAX else None


def _split(u: dict) -> tuple[int | None, int | None]:
    """Cache writes by TTL as the response split them; unknown when it did not."""
    cc = u.get("cache_creation") or {}
    return _n(cc, "ephemeral_5m_input_tokens"), _n(cc, "ephemeral_1h_input_tokens")


def _sum(spent: list[dict], read) -> int | None:
    """Summed over the attempts that reported the figure; None when none did."""
    known = [v for v in (read(s) for s in spent) if v is not None]
    return sum(known) if known else None


def _add(*counts: int | None) -> int | None:
    return sum(counts) if all(c is not None for c in counts) else None  # type: ignore[arg-type]


def _prompt(u: dict) -> int | None:
    """The prompt count: fresh input, cache read and cache write together (ADR-0009/C2); None when any
    of the three is unknown or they sum to zero, since no request is sent with an empty prompt and a
    non-positive figure anchors nothing."""
    p = _add(_n(u, "input_tokens"), _n(u, "cache_read_input_tokens"), _n(u, "cache_creation_input_tokens"))
    return p or None


def _usage(spent: list[dict]) -> dict:
    """Summed over the attempts that reported each figure, in the shape the router and the CLI report,
    so the bench reads all alike; a figure no attempt reported is None. The cache write split beside it,
    None when no response split it."""
    inp, read, write, out = (_sum(spent, lambda s, k=k: _n(s, k)) for k in COUNTS)
    w5 = _sum(spent, lambda s: _split(s)[0])
    w1h = _sum(spent, lambda s: _split(s)[1])
    thinking = _sum(spent, lambda s: _n(s.get("output_tokens_details") or {}, "thinking_tokens"))
    return {
        "prompt_tokens": _add(inp, read, write) or None,
        "completion_tokens": out,
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
        "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1h},
        "output_tokens": out,
        "reasoning_output_tokens": thinking,
    }


def _counted(spent: list[dict]) -> bool:
    """Whether every attempt reported all four counts and a prompt (`_prompt`), so the usage above is the
    whole."""
    return all(_n(s, k) is not None for s in spent for k in COUNTS) and all(_prompt(s) for s in spent)


def _prices(u: dict) -> tuple[float, float, float, float, float] | None:
    tiers = PRICES.get(str(u.get("model") or ""))
    if not tiers:
        return None
    prompt = _prompt(u)
    if prompt is None:
        return None
    for end, prices in tiers:
        if end is None or prompt <= end:
            return prices
    return None


def _tier(u: dict) -> str | None:
    """Which price band the attempt fell in, for a model priced by prompt length; None for the rest."""
    tiers = PRICES.get(str(u.get("model") or ""))
    if not tiers or len(tiers) == 1:
        return None
    prompt = _prompt(u)
    if prompt is None:
        return None
    for end, _ in tiers:
        if end is None:
            return f"over {tiers[-2][0]}"
        if prompt <= end:
            return f"up to {end}"
    return None


def _attempt_cost(u: dict) -> float | None:
    """What one attempt cost at the served model's prices and its tier; None when the table lacks the
    model or any of the four counts is unknown. A write the response did not split at all is priced at
    the request's one TTL, which every breakpoint carried (ADR-0028/C1, C2)."""
    counts = [_n(u, k) for k in COUNTS]
    prices = _prices(u)
    if prices is None or any(c is None for c in counts):
        return None
    inp, read, write, out = counts
    base, p5, p1h, p_read, p_out = prices
    if "cache_creation" not in u:
        if u.get("ttl") not in TTLS:
            return None
        w5, w1h = (write, 0) if u["ttl"] == "5m" else (0, write)
    else:
        # a split the response carries is read as it is: half of one, one not a count, or one not
        # summing to the write it splits certifies nothing, and the bill stays open
        w5, w1h = _split(u)
        if w5 is None or w1h is None or w5 + w1h != write:
            return None
    return (inp * base + w5 * p5 + w1h * p1h + read * p_read + out * p_out) / 1_000_000  # type: ignore[operator]


def _cost(spent: list[dict]) -> float | None:
    """The known subtotal; `_complete` says whether every attempt is in it."""
    costs = [c for c in (_attempt_cost(s) for s in spent) if c is not None]
    return sum(costs) if costs else None


def _complete(spent: list[dict]) -> bool:
    return all(_attempt_cost(s) is not None for s in spent)


def _limits(headers: dict) -> dict:
    """The rate-limit headers of the answering response, and its request id."""
    return {k: v for k, v in headers.items() if k.startswith("anthropic-ratelimit-") or k == "request-id"}


def _transient(message: str) -> bool:
    return message.startswith(("HTTP 429", "HTTP 5", "request failed"))


def _wait(attempt: int, retry_after: float | None) -> float:
    """The backoff, or the provider's `Retry-After` when it gave one, either within the one bound."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), _RETRY_AFTER_MAX)
    return min(2.0 * 2**attempt, _RETRY_AFTER_MAX)


def _retry_after(headers: dict) -> float | None:
    """`Retry-After` in seconds, or as an HTTP date read against now; None when absent or unreadable."""
    v = headers.get("retry-after")
    if v is None:
        return None
    try:
        delay = float(v)
    except ValueError:
        pass
    else:
        return delay if math.isfinite(delay) else None  # `nan` would wait forever, `inf` is no wait
    try:
        at = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.UTC)
    return max((at - datetime.datetime.now(datetime.UTC)).total_seconds(), 0.0)


def _post(
    payload: bytes, timeout: float, key: str, heard: Callable[[dict], None] | None = None
) -> tuple[dict, dict]:
    """One request under the key; the parsed body and the response headers, lowercased. Everything the
    provider sent back, the body, the headers and the text of any error, is redacted of the key before
    it leaves here; a body that needed it says so (`key_redacted`). `heard` takes the headers as soon as
    they are read, so a call that ends while the body is still coming names its response."""
    windows = _windows(key)
    req = urllib.request.Request(
        URL,
        data=payload,
        headers={"x-api-key": key, "anthropic-version": VERSION, "content-type": "application/json"},
        method="POST",
    )
    headers: dict = {}
    # a header's name is case-insensitive and kept lowered, so it is matched in any case in one pass:
    # a hop may hand the key in it case-folded, and lowering only after redacting would let it through
    folded = {w: frozenset(f.lower() for f in fs) for w, fs in windows.items()}

    def read(items) -> dict:
        return {_redact(k.lower(), key.lower(), folded): _redact(str(v), key, windows) for k, v in items}

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # the headers first: a body cut mid-read still names its response on the failed envelope
            headers = read(resp.headers.items())
            if heard is not None:
                heard(headers)
            raw = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        # the status and headers first: a refusal whose body is cut mid-read is still that refusal
        headers = read(e.headers.items())
        if heard is not None:
            heard(headers)
        try:
            # redacted whole, then cut: cut first, an echo across the 500th character kept its start
            detail = _redact(e.read().decode(errors="replace"), key, windows)[:500]
        except (http.client.HTTPException, OSError) as cut:
            detail = f"(the body could not be read: {type(cut).__name__})"
        failed = AnthropicError(
            f"HTTP {e.code}: {detail}", status=e.code, retry_after=_retry_after(headers), headers=headers
        )
    except urllib.error.URLError as e:
        # the transport's text can carry a proxy's or a server's line: redacted like a body
        failed = AnthropicError(f"request failed: {_redact(str(e.reason), key, windows)}")
    except (http.client.HTTPException, OSError) as e:
        # a response cut mid-body or a socket that reset: transient, retried like a 503
        failed = AnthropicError(
            f"request failed: {type(e).__name__}: {_redact(str(e), key, windows)}", headers=headers
        )
    else:
        failed = None
    if failed is not None:
        # raised past the handler, so the error keeps no context: the exception it replaces, whose text
        # or body the provider wrote, is unredacted (ADR-0009 S32)
        raise failed
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        data = None  # not JSON, an integer past the parser's digit limit, or nested past its depth
    if _readable(data):
        try:
            data, redacted = _scrubbed(data, key, windows)
        except RecursionError:
            data = None
        else:
            if redacted:
                data["key_redacted"] = True
            return data, headers
    # an answer nothing can read: transient like a cut body, and the billed attempts before it stay; its
    # headers are kept, so the failed envelope names this response, not an earlier one
    raise AnthropicError(
        f"request failed: unreadable response body: {_redact(raw, key, windows)[:500]}", headers=headers
    )


def _readable(data: object) -> bool:
    """Whether the body is a message in the shape the backend reads: content a list of blocks, each text
    block's text a string, usage, when present, a table (a usage of 0 is refused though the reader
    would take it as none), the stop reason text; a 200 in another shape, or without content, is a
    failed attempt, not an exception and not a blank answer."""
    if not isinstance(data, dict):
        return False
    content, usage = data.get("content"), data.get("usage")
    return (
        isinstance(content, list)
        and all(
            isinstance(b, dict) and (b.get("type") != "text" or isinstance(b.get("text"), str))
            for b in content
        )
        and (usage is None or isinstance(usage, dict))
        and isinstance(data.get("stop_reason"), str | None)
    )
