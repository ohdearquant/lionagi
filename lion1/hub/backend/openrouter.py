"""OpenRouter as a chat-completion backend (ADR-0009): the view in as a real message array, text out.

Unlike the CLI shapes, this one sends the view as separate messages with their roles, so the
provider's prefix cache can hit on everything that did not change since the last round. Usage and
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

from lionagi.actor import Message, Reply

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

    async def __call__(self, messages: list[Message]) -> str:
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
        t0 = time.monotonic()
        spent: list[dict] = []  # every response's usage, so a retried attempt's bill is not lost
        posting = False  # a request is out: cancelled there, its bill is unknown, not absent
        try:
            for attempt in range(self.retries + 1):
                try:
                    posting = True
                    data = await asyncio.to_thread(_post, body, self.timeout)
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
        except CancelledError:
            if posting:
                spent.append({})
            self._bill_failure(spent, "cancelled", len(spent), t0)  # nothing spent: nothing to bill
            raise
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
        self.calls.append(envelope)
        # the prompt the answering attempt sent; the envelope's usage sums every attempt for the bill
        # the provider's own markup around a tag is rewritten to the tag before the runtime reads it
        return Reply(normalize(text), prompt_tokens=(data.get("usage") or {}).get("prompt_tokens"))

    def _bill_failure(self, spent: list[dict], error: str, attempts: int, t0: float) -> None:
        """A call that ends in an error after the provider billed, or may have billed, an attempt (a
        blank answer, then the transport failing) still lands on `calls`, as a failed envelope carrying
        what was observed: the cost was spent whether or not the call returned (ADR-0009/C5)."""
        if not spent:
            return
        self.calls.append(
            {
                "error": error,
                "attempts": attempts,
                "duration_api_ms": int((time.monotonic() - t0) * 1000),
                "usage": _usage(spent, {}),
                "total_cost_usd": _cost(spent),
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


def _post(body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ohdearquant/lionagi",
            "X-Title": "lionagi v1 bench",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise OpenRouterError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise OpenRouterError(f"request failed: {e.reason}") from None
    except (http.client.IncompleteRead, ConnectionError, TimeoutError) as e:
        # a response cut mid-body or a socket that reset: transient, retried like a 503 (run-2 of the
        # hard45 unaided arm lost two instances at their first call to an uncaught IncompleteRead)
        raise OpenRouterError(f"request failed: {type(e).__name__}: {e}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        # an answer nothing can read: transient like a cut body, and the billed attempts before it stay
        raise OpenRouterError(f"request failed: unreadable response body: {raw}")
    return data
