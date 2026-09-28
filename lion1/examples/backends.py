"""Choosing a backend, and reading what each call cost.

A backend is a function from the view (a list of messages) to the model's text; everything else
is the runtime's. The loop never depends on native tool calling: a native tool call in a response
is read back as the text the model wrote. Four are built. OpenRouter sends the view as a real
message array so the provider's prefix cache hits on the unchanged prefix; for DeepSeek models the
provider is pinned to DeepInfra with Fireworks as the only fallback and data collection denied (the
first-party endpoint trains on traffic, and provider slugs are lowercase), the model's own thinking
runs under a token budget (2000 on the bench: 42 of 50 against 33 with thinking off, ADR-0012/C7),
and the completion stops where the model starts writing the loop's own `<system round=` frame.
Codex runs `codex exec` with every acting feature off, on a subscription (no cost figure). Claude
Code runs `claude -p` with tools off. `claude` resumes one CLI session per run and sends only what
the view gained; `session` is the same shape under its own name. `claude-oneshot` is one fresh
`claude -p` call per round, the whole view sent as a transcript each time.

    uv run python examples/backends.py --backend openrouter [--model deepseek/deepseek-v4.1-flash]
    uv run python examples/backends.py --backend codex [--model gpt-6-luna]
    uv run python examples/backends.py --backend claude | session | claude-oneshot
"""

from __future__ import annotations

import argparse
import asyncio

from pydantic import BaseModel, Field

from hub.harness import (
    ClaudeCode,
    ClaudeCodeError,
    ClaudeCodeSession,
    Codex,
    CodexError,
    OpenRouter,
    OpenRouterError,
)
from lionagi import Actor, Context, Kind, Profile, RunRequest, Success

TEXT = "the loop goes round and every round is one message in and one message out"


class WordCount(BaseModel):
    """Count the words in a text."""

    text: str


class Longest(BaseModel):
    """The longest word in a text."""

    text: str


class Report(BaseModel):
    words: int = Field(description="the word count")
    longest: str = Field(description="the longest word")


def build_backend(kind: str, model: str | None):
    if kind == "openrouter":
        model = model or "deepseek/deepseek-v4.1-flash"
        provider = (
            {"order": ["deepinfra", "fireworks"], "allow_fallbacks": False, "data_collection": "deny"}
            if model.startswith("deepseek/")
            else None
        )
        return OpenRouter(
            model=model,
            timeout=120,
            provider=provider,
            reasoning={"max_tokens": 2000},
            stop=["<system round="],
        )
    if kind == "codex":
        return Codex(model=model or "gpt-6-luna", timeout=300)
    if kind == "claude-oneshot":
        return ClaudeCode(model=model)
    return ClaudeCodeSession(model=model)  # "claude" and "session" are the same shape


async def main(kind: str, model: str | None) -> None:
    actor = Actor("counter")

    @actor.handler(WordCount)
    async def word_count(req: WordCount, ctx: Context) -> int:
        return len(req.text.split())

    @actor.handler(Longest)
    async def longest(req: Longest, ctx: Context) -> str:
        return max(req.text.split(), key=len)

    backend = build_backend(kind, model)
    try:
        run = await actor.run(
            Profile(
                "counter",
                system="Use the commands; point at values; finish with OUT{} when both numbers are in.",
            ),
            RunRequest(
                inputs=(f"How many words, and which is the longest? Text: {TEXT!r}",),
                emits=(Report,),
                max_rounds=6,
                time_budget=180,
            ),
            backend,
        )
    except BaseException:  # an interrupt too: the calls that completed before it were paid for
        if backend.calls:
            spent(backend)
        raise
    print(f"{kind}: {type(run.outcome).__name__} after {run.round} rounds")
    for e in run.record:
        if e.kind is Kind.TEXT:
            print(f"  [{e.name}] {e.content.strip()[:160]!r}")
    if isinstance(run.outcome, Success):
        print("  output:", run.output["report"].model_dump())
    spent(backend)


def spent(backend) -> None:
    """One line per call the backend made, whatever the run came to."""
    print(
        f"\n{'call':>4} {'ms':>6} {'input':>6} {'cached':>6} {'output':>6} {'think':>6} "
        f"{'cost $':>9}  finish / provider"
    )

    def cost(v, complete) -> str:
        # a subtotal with an attempt uncounted (the call was cut off, or an attempt answered without
        # a price) is at least what was counted, and says so
        return "unknown" if v is None else f"{'' if complete is not False else '>='}{float(v):.5f}"

    for i, c in enumerate(backend.calls, 1):
        u = c.get("usage") or {}
        ms = c.get("duration_api_ms") or c.get("duration_ms") or 0
        print(
            f"{i:>4} {ms:>6} {u.get('input_tokens', 0):>6} "
            f"{u.get('cache_read_input_tokens', 0):>6} {u.get('output_tokens', 0):>6} "
            f"{u.get('reasoning_output_tokens') or 0:>6} "
            f"{cost(c.get('total_cost_usd'), c.get('cost_complete')):>9}  "
            + (  # one line per call: a CLI's error can run to several
                c["error"].splitlines()[0]
                if c.get("error")
                else f"{c.get('finish_reason')} / {c.get('provider')}"
            )
        )
    print("cost is None on a subscription CLI: the spend is unknown, not zero; >= marks a subtotal")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--backend",
        choices=["openrouter", "codex", "claude", "session", "claude-oneshot"],
        default="openrouter",
    )
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    try:
        asyncio.run(main(a.backend, a.model))
    except (OpenRouterError, ClaudeCodeError, CodexError) as e:
        # no key, or no CLI on PATH: the backend says which, and the example stops there
        raise SystemExit(f"{a.backend}: {e}") from None
