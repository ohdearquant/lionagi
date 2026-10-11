"""OpenRouter as a chat-completion backend (ADR-0009): the view in as a real message array, text out.

Unlike the CLI shapes, this one sends the view as separate messages with their roles, so the
provider's prefix cache can hit on everything that did not change since the last turn. Usage and
cost come back on the response (`usage.cost` when `usage: {include: true}` is asked for), so the
bench reads them off the envelope like the CLI's.

The key is `OPENROUTER_API_KEY` in the environment, else the macOS keychain item of that service
name under account `OPENROUTER_KEYCHAIN_ACCOUNT` (default `khive`; the keychain holds several and the
unqualified lookup returned an expired one). It is never logged.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from asyncio import CancelledError  # by name: tests stand in for the module's `asyncio`
from dataclasses import dataclass, field

from lion1.actor import Message, Reply

from .anthropic import _KEY, _KEY_FORM, _redact, _scrubbed, _windows
from .markup import normalize, tool_calls_text

__all__ = (
    "URL",
    "OpenRouterError",
    "api_key",
    "OpenRouter",
)

URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ.get("OPENROUTER_KEYCHAIN_ACCOUNT", "khive"),
                "-s",
                "OPENROUTER_API_KEY",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is None or out.returncode or not out.stdout.strip():
        raise OpenRouterError("OPENROUTER_API_KEY: not in the environment and not in the keychain")
    return out.stdout.strip()


@dataclass
class OpenRouter:
    model: str
    timeout: float = 300.0
    # a turn is a whole program, so the cap is sized for one: at 8192, 3 of 125 turns that wrote
    # 40 to 52 actions were cut mid-tag on the first ten SWE-bench instances
    max_tokens: int = 32768
    temperature: float | None = None
    provider: dict | None = None  # OpenRouter provider routing, e.g. {"order": ["DeepSeek"]}
    reasoning: dict | None = None  # OpenRouter reasoning control, e.g. {"enabled": False}
    stop: list[str] | None = None  # sequences that end the completion; the text before them is the answer
    retries: int = 4  # on a transient failure (429, 5xx, an empty or errored choice), with backoff
    calls: list[dict] = field(default_factory=list)  # response envelope per call, minus the text

    def body(self, messages: list[Message]) -> dict:
        body: dict = {
            "model": self.model,
            # only the head is a system message; a SYSTEM notification mid-conversation goes as user,
            # which every OpenAI-compatible provider accepts
            "messages": [
                {
                    "role": _role(m) if i == 0 else ("user" if m.role == "system" else _role(m)),
                    "content": m.content,
                }
                for i, m in enumerate(messages)
            ],
            "max_tokens": self.max_tokens,
            "usage": {"include": True},
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.provider:
            body["provider"] = self.provider
        if self.reasoning is not None:
            body["reasoning"] = self.reasoning
        if self.stop:
            body["stop"] = self.stop
        return body

    async def __call__(self, messages: list[Message]) -> str:
        t0 = time.monotonic()
        try:
            body = self.body(messages)
            key = api_key()
            if not _KEY.fullmatch(key):
                # refused as the Messages backend refuses it: a trailing newline failed inside the
                # request, which counted it as sent
                raise OpenRouterError(f"the OpenRouter key is not {_KEY_FORM}")
            payload = json.dumps(body).encode()
        except Exception as e:
            # refused here, nothing sent: the envelope says so, with a known zero (ADR-0009/C5)
            unsent = (
                str(e)
                if isinstance(e, OpenRouterError)
                else f"the request could not be built: {type(e).__name__}"
            )
            self._bill_failure([], unsent, 0, t0)
        else:
            unsent = None
        if unsent is not None:
            raise OpenRouterError(unsent)  # past the handler, with no context (ADR-0009 S32)
        spent: list[dict] = []  # every response's usage, so a retried attempt's bill is not lost
        posting = False  # a request is out: cancelled there, its bill is unknown, not absent
        sent = 0  # requests sent, the attempts a cancelled or unreadable call reports
        try:
            for attempt in range(self.retries + 1):
                try:
                    posting = True
                    sent += 1
                    data = await asyncio.to_thread(_post, payload, key, self.timeout)
                except OpenRouterError as e:
                    posting = False
                    if str(e).startswith("request failed"):
                        spent.append(
                            {}
                        )  # it went out and no readable answer came back: billed or not is unknown
                    if attempt < self.retries and _transient(str(e)):
                        await asyncio.sleep(2.0 * 2**attempt)
                        continue
                    self._bill_failure(spent, str(e), attempt + 1, t0)
                    raise
                posting = False
                spent.append(data.get("usage") or {})
                if data.get("error"):
                    # a 200 whose body is the provider's error, not a choice: named by its own code and
                    # retried only when that code is transient, never because its prose says timeout
                    err = data["error"] if isinstance(data["error"], dict) else {"message": data["error"]}
                    error = f"error {err.get('code')}: {err.get('message')}"
                    if err.get("metadata"):
                        error += f" {json.dumps(err['metadata'])}"
                    if attempt < self.retries and _transient(f"HTTP {err.get('code')}"):
                        await asyncio.sleep(2.0 * 2**attempt)
                        continue
                    self._bill_failure(spent, error, attempt + 1, t0)
                    raise OpenRouterError(error)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                text = message.get("content") or ""
                finish = choice.get("finish_reason")
                if not text and message.get("tool_calls"):
                    # No tools were offered, so the model wrote its turn in its native tool-call format
                    # and the provider parsed it out of the content. That is the model's turn: the tag
                    # text sits in the call's name, and it is handed back as the text the model wrote
                    # (40 of 952 Mini-50 turns came this way; as a JSON dump 21 of them were unreadable)
                    text = tool_calls_text(message["tool_calls"])
                if text or finish == "length":
                    break
                if finish == "stop":
                    # a finished answer with nothing in it: the provider billed 80 to 220 output tokens
                    # and returned content, reasoning and refusal all null (about 1 call in 100 with
                    # thinking off, mostly the second turn). There is nothing to read, so sample again;
                    # if it stays empty the loop is told a blank turn came back and goes on
                    if attempt < self.retries:
                        await asyncio.sleep(1.0)
                        continue
                    break
                # an errored choice: the provider's fault, not the model's turn
                if attempt < self.retries:
                    await asyncio.sleep(2.0 * 2**attempt)
                    continue
                error = f"empty answer after {attempt + 1} attempts, finish_reason={finish}"
                self._bill_failure(spent, error, attempt + 1, t0)
                raise OpenRouterError(error)
            envelope = {k: v for k, v in data.items() if k != "choices"}
            envelope["finish_reason"] = choice.get("finish_reason")
            envelope["native_finish_reason"] = choice.get("native_finish_reason")
            if message.get("tool_calls"):
                envelope["tool_calls"] = message["tool_calls"]
            if not text.strip():
                # nothing readable came back: keep the whole message, so the turn can be diagnosed
                # from the record instead of reproduced (12 of 1022 Mini-50 turns came back blank
                # with 37 to 140 output tokens and no finish reason, and nothing said what they were)
                envelope["message"] = message
            envelope["attempts"] = attempt + 1
            envelope["duration_api_ms"] = int((time.monotonic() - t0) * 1000)  # retries and backoff included
            envelope["usage"] = _usage(spent, data.get("usage") or {})
            envelope["total_cost_usd"] = _cost(spent)
            envelope["cost_complete"] = _complete(spent)
            if len(spent) > 1:
                envelope["attempt_usage"] = spent
            refused = None
            if message.get("content") and message.get("tool_calls"):
                # the model's turn in both channels: read by halves, either could run without the other,
                # so none of it runs and the model is told (ADR-0004/C4); the envelope keeps the calls
                refused = (
                    "the turn came back in two channels, as text and as a native tool call, and nothing "
                    "in it was executed: write the whole turn as text, the tags included; this "
                    "conversation has no tool-call API"
                )
            # the prompt the answering attempt sent; the envelope's usage sums every attempt for the bill
            # the provider's own markup around a tag is rewritten to the tag before the runtime reads it
            reply = Reply(
                normalize(text), prompt_tokens=(data.get("usage") or {}).get("prompt_tokens"), refused=refused
            )
        except CancelledError:
            if posting:
                spent.append({})
            self._bill_failure(spent, "cancelled", sent, t0)
            raise
        except OpenRouterError:
            raise
        except Exception as e:
            # anything else still lands a failed envelope (ADR-0009/C5); the error names the class only,
            # since its text may carry what the provider sent, and a request out when it was raised
            # may have been billed
            if posting:
                spent.append({})
            unread = f"the response could not be read: {type(e).__name__}"
            self._bill_failure(spent, unread, sent, t0)
        else:
            self.calls.append(envelope)
            return reply
        raise OpenRouterError(unread)  # past the handler, with no context (ADR-0009 S32)

    def _bill_failure(self, spent: list[dict], error: str, attempts: int, t0: float) -> None:
        """Every call that ends in an error lands on `calls` as a failed envelope carrying what was
        observed (ADR-0009/C5): the attempts the provider billed, or may have billed (a blank answer,
        then the transport failing); none when it was refused before any, whose cost is a complete
        zero."""
        self.calls.append(
            {
                "error": error,
                "attempts": attempts,
                "duration_api_ms": int((time.monotonic() - t0) * 1000),
                "usage": _usage(spent, {}),
                "total_cost_usd": _cost(spent) if spent else 0.0,
                "cost_complete": _complete(spent),
                "attempt_usage": spent,
            }
        )


def _usage(spent: list[dict], last: dict) -> dict:
    """Summed over every attempt: a sampled-again blank answer was billed too. The same shape the CLI
    reports, so the bench reads both alike."""
    prompt = sum(int(s.get("prompt_tokens") or 0) for s in spent)
    cached = sum(int((s.get("prompt_tokens_details") or {}).get("cached_tokens") or 0) for s in spent)
    completion = sum(int(s.get("completion_tokens") or 0) for s in spent)
    reasoning = [(s.get("completion_tokens_details") or {}).get("reasoning_tokens") for s in spent]
    return {
        **last,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "input_tokens": prompt - cached,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "output_tokens": completion,
        "reasoning_output_tokens": (
            sum(r for r in reasoning if r is not None) if any(r is not None for r in reasoning) else None
        ),
    }


def _cost(spent: list[dict]) -> float | None:
    """The known subtotal; `_complete` says whether every attempt is in it."""
    costs = [s.get("cost") for s in spent if s.get("cost") is not None]
    return sum(costs) if costs else None


def _complete(spent: list[dict]) -> bool:
    return all(s.get("cost") is not None for s in spent)


def _transient(message: str) -> bool:
    return message.startswith(("HTTP 429", "HTTP 5", "request failed"))


def _role(m: Message) -> str:
    return m.role if m.role in ("system", "user", "assistant") else "user"


def _post(payload: bytes, key: str, timeout: float) -> dict:
    """One request. The provider's text in an error or an answer is redacted of the key, as the Messages
    backend redacts it (ADR-0009 S32), and an answer in a shape `__call__` cannot read is a failed attempt."""
    windows = _windows(key)
    req = urllib.request.Request(
        URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ohdearquant/lionagi",
            "X-Title": "lion1 bench",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        # the status first: a refusal whose body is cut mid-read is still that refusal, and retried as one
        try:
            # redacted whole, then cut: cut first, an echo across the 500th character kept its start
            detail = _redact(e.read().decode(errors="replace"), key, windows)[:500]
        except (http.client.HTTPException, OSError) as cut:
            detail = f"(the body could not be read: {type(cut).__name__})"
        error = f"HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        # the transport's text can carry a proxy's or a server's line: redacted like a body
        error = f"request failed: {_redact(str(e.reason), key, windows)}"
    except (http.client.HTTPException, OSError) as e:
        # a response cut mid-body, a garbled status line, a socket that reset or a TLS stream ended
        # early: transient, retried like a 503 (run-2 of the hard45 unaided arm lost two instances at
        # their first call to an uncaught IncompleteRead; an SSLEOFError or a BadStatusLine escaped
        # the same way, with no envelope)
        error = f"request failed: {type(e).__name__}: {_redact(str(e), key, windows)}"
    else:
        error = None
    if error is not None:
        # raised past the handler, so the error keeps no context: the exception it replaces, whose text
        # or body the provider wrote, is unredacted (ADR-0009 S32)
        raise OpenRouterError(error)
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
            return data
    # an answer nothing can read: transient like a cut body, and the billed attempts before it stay
    raise OpenRouterError(f"request failed: unreadable response body: {_redact(raw, key, windows)[:500]}")


_COUNTS = ("prompt_tokens", "completion_tokens")


def _readable(data: object) -> bool:
    """Whether the body is an answer in the shape `__call__` reads: the first choice a table whose message
    is a table, its content text, its tool calls a list; usage, when present, a table of counts and a
    cost. Stricter than the reader on purpose: a count is an integer, so a body writing 12.0 or a usage
    of 0 is refused though `__call__` could read it. A 200 in another shape is a failed attempt whose
    bill is unknown, not an exception."""
    if not isinstance(data, dict):
        return False
    usage = data.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            return False
        details = [usage.get("prompt_tokens_details"), usage.get("completion_tokens_details")]
        if any(d is not None and not isinstance(d, dict) for d in details):
            return False
        counts = [usage.get(k) for k in _COUNTS] + [
            (details[0] or {}).get("cached_tokens"),
            (details[1] or {}).get("reasoning_tokens"),
        ]
        if any(n is not None and (type(n) is not int or n < 0) for n in counts):
            return False
        cost = usage.get("cost")
        # at most 1e300, so a sum of a call's attempts stays a float: an integer of 400 digits passed
        # `< inf`, and the sum raised in the failure path too, which then landed no envelope
        if cost is not None and (type(cost) not in (int, float) or not 0 <= cost <= 1e300):
            return False
    if data.get("error"):
        return True
    choices = data.get("choices")
    if choices is None:
        return True
    if not isinstance(choices, list):
        return False
    if not choices:
        return True
    choice = choices[0]
    if not isinstance(choice, dict):
        return False
    message = choice.get("message")
    if message is None:
        return True
    if not isinstance(message, dict):
        return False
    if message.get("content") is not None and not isinstance(message["content"], str):
        return False
    calls = message.get("tool_calls")
    if calls is None:
        return True
    # each call a table whose function is a table, as `tool_calls_text` reads them
    return isinstance(calls, list) and all(
        isinstance(c, dict) and (c.get("function") is None or isinstance(c["function"], dict)) for c in calls
    )
