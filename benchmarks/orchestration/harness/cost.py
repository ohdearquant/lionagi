"""Provider-agnostic cost & compute accounting.

The matched-compute confound (arxiv 2604.02460) is the #1 reason a multi-agent
"win" is meaningless: multi-agent spends 2-4x the tokens, so any lift must be
reported PER DOLLAR, not per task. HAL (arxiv 2510.11977) is the standard:
report accuracy alongside tokens, USD, and latency.

Three facts shape this module (all verified against current provider docs,
2026-06-01):

1. **Subscription auth gives no usable USD.** codex / claude_code run via a
   ChatGPT/Claude subscription report ``total_cost_usd = 0`` (or omit it). So we
   never trust provider-reported dollars. The hard number is TOKENS; USD is
   ``tokens x published_price`` via the editable table below. Override prices
   without editing code via ``LIONAGI_BENCH_PRICES`` (see ``_prices``).

2. **Re-tokenizing the transcript undercounts CLI agents.** A codex ``operate``
   is one subprocess that does many internal turns (file reads, tool calls)
   invisible to lionagi's message history. The accurate source is provider usage
   in the terminal ``result`` chunk, which lionagi now stamps onto
   ``AssistantResponse.metadata["model_response"]`` (operations/run/run.py).
   Tokenizing prompts+outputs is the fallback only.

3. **codex hides reasoning tokens (THE cross-provider bias).** ``codex exec
   --json`` reports ``{input_tokens, cached_input_tokens, output_tokens}`` where
   ``output_tokens`` EXCLUDES reasoning (github.com/openai/codex/issues/19022).
   reasoning_output_tokens lives only in the on-disk rollout, never the stream.
   Anthropic, by contrast, counts thinking tokens INSIDE output_tokens. So codex
   $ from the stream is a LOWER BOUND while claude $ is complete — a naive
   small(codex)-vs-big(claude) comparison flatters codex. We flag this via
   ``reasoning_disclosed``; recovering exact reasoning from the rollout file is a
   tracked follow-up. Until then, treat codex cost as a floor.

Token-convention difference handled in ``_norm_tokens``:
  - OpenAI: ``input_tokens`` is TOTAL prompt incl. cached; ``cached_input_tokens``
    is the cached subset (billed cheaper). uncached = input - cached.
  - Anthropic: ``input_tokens`` is uncached only; ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` are separate.

Prices are USD per 1,000,000 tokens: ``(input, output, cached_input)``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from lionagi.service.token_calculator import TokenCalculator

# --- Published price table (USD / 1M tokens): (input, output, cached_input) ---
# VERIFIED 2026-06-01. Re-check before quoting USD externally; tokens are exact,
# these only convert tokens -> USD and re-run trivially at new values. Keys match
# the canonical CLI specs the runner resolves to (runner._MODEL_ALIASES).
_DEFAULT_PRICES: dict[str, tuple[float, float, float]] = {
    # OpenAI gpt-5.4-mini — pricepertoken.com / OpenAI API, 2026-06-01.
    # NOTE: codex stream output EXCLUDES reasoning (billed at the output rate),
    # so any codex USD here is a lower bound — see module docstring fact 3.
    "codex/gpt-5.4-mini": (0.75, 4.50, 0.075),
    "openai/gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    # codex spark is NOT publicly available / priced. Proxy at mini's price so
    # token-normalized runs still work, but any spark USD is a PROXY — flag it.
    # Prefer gpt-5.4-mini as the cheap-model rep for market-legible numbers.
    "codex/gpt-5.3-codex-spark": (0.75, 4.50, 0.075),
    "gpt-5.3-codex-spark": (0.75, 4.50, 0.075),
    "codex": (0.75, 4.50, 0.075),
    # Anthropic via claude_code CLI. Output_tokens INCLUDES thinking (accurate).
    # cached = cache-read rate (~0.1x input). VERIFY against current Anthropic
    # pricing before external quotes.
    "claude-code/haiku": (1.00, 5.00, 0.10),
    "claude-code/sonnet": (3.00, 15.00, 0.30),
    "claude-code/opus": (15.00, 75.00, 1.50),
}

# spark's USD is a proxy (not public) — surfaced so writeups can flag it.
PROXY_PRICED = {"openai/gpt-5.3-codex-spark", "gpt-5.3-codex-spark", "codex"}


def _prices() -> dict[str, tuple[float, float, float]]:
    table = dict(_DEFAULT_PRICES)
    override = os.environ.get("LIONAGI_BENCH_PRICES")
    if override:
        for k, v in json.loads(override).items():
            cached = float(v[2]) if len(v) > 2 else float(v[0]) * 0.1
            table[k] = (float(v[0]), float(v[1]), cached)
    return table


@dataclass(slots=True)
class Usage:
    """Accumulated compute for one run, summed across ALL agents/branches.

    Token fields are billing-ready: ``input_tokens`` is UNCACHED prompt tokens,
    ``cached_tokens`` billed at the (cheaper) cached rate, ``output_tokens`` the
    completion. ``reasoning_disclosed`` is False whenever any codex call
    contributed (its reasoning is unbilled-here, so cost is a floor)."""

    input_tokens: int = 0  # uncached prompt tokens (billed at input rate)
    cached_tokens: int = 0  # cached prompt tokens (billed at cached rate)
    output_tokens: int = 0  # completion (incl. reasoning for Anthropic, NOT codex)
    num_turns: int = 0  # internal CLI turns (tool calls etc.), if reported
    n_calls: int = 0  # lionagi-level model invocations seen
    source: str = "none"  # reported | estimated | mixed | none
    reasoning_disclosed: bool = True  # False if any codex call (reasoning hidden)
    # model -> [uncached_in, cached_in, out] for exact (and mixed-model) pricing
    per_model: dict[str, list[int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cached_tokens + self.output_tokens

    def _add(self, model: str, inp: int, cached: int, out: int) -> None:
        self.input_tokens += inp
        self.cached_tokens += cached
        self.output_tokens += out
        slot = self.per_model.setdefault(model, [0, 0, 0])
        slot[0] += inp
        slot[1] += cached
        slot[2] += out

    def cost_usd(self, default_model: str) -> float:
        """USD via the price table, billed per-model where tracked. For codex,
        reasoning is excluded from output (see module docstring) so this is a
        lower bound when ``reasoning_disclosed`` is False."""
        table = _prices()
        models = self.per_model or {
            default_model: [self.input_tokens, self.cached_tokens, self.output_tokens]
        }
        total = 0.0
        for model, (inp, cached, out) in models.items():
            pin, pout, pcached = table.get(model, table.get(default_model, (0.0, 0.0, 0.0)))
            total += inp / 1e6 * pin + cached / 1e6 * pcached + out / 1e6 * pout
        return round(total, 6)


def cost_of(input_tokens: int, cached_tokens: int, output_tokens: int, model: str) -> float:
    """USD for a single run's aggregate tokens at the model's published price.
    For codex this is a lower bound (reasoning excluded — see module docstring)."""
    table = _prices()
    pin, pout, pcached = table.get(model, (0.0, 0.0, 0.0))
    return round(
        input_tokens / 1e6 * pin + cached_tokens / 1e6 * pcached + output_tokens / 1e6 * pout,
        6,
    )


def _dig_usage(mr: dict) -> dict | None:
    """Pull a usage dict out of a model_response of unknown provider shape."""
    if not isinstance(mr, dict):
        return None
    if isinstance(mr.get("usage"), dict):
        return mr["usage"]
    token_keys = (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    )
    if any(k in mr for k in token_keys):
        return mr
    return None


def _norm_tokens(u: dict) -> tuple[int, int, int, bool]:
    """Normalize a provider usage dict to (uncached_in, cached_in, out, codex?).

    Handles the OpenAI vs Anthropic input-token convention difference."""
    out = int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0)
    if "cached_input_tokens" in u:  # OpenAI/codex: input_tokens is TOTAL incl cached
        total_in = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
        cached = int(u.get("cached_input_tokens", 0) or 0)
        uncached = max(0, total_in - cached)
        return uncached, cached, out, True
    # Anthropic: input_tokens is uncached; cache_* are separate
    uncached = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
    cached = int(u.get("cache_read_input_tokens", 0) or 0) + int(
        u.get("cache_creation_input_tokens", 0) or 0
    )
    if not uncached and not out and u.get("total_tokens"):
        out = int(u["total_tokens"])  # only a total — attribute to output (conservative)
    return uncached, cached, out, False


def collect_usage(branches, prompts_and_outputs, default_model: str) -> Usage:
    """Sum provider-reported usage across every AssistantResponse in every
    branch. Falls back to tokenizing prompts+outputs when no branch reported
    usage. ``branches`` is an iterable of lionagi Branch; ``prompts_and_outputs``
    is [(prompt, output_text), ...] used only for the estimate fallback."""
    usage = Usage()
    reported_any = False
    estimated_any = False

    for br in branches:
        model_name = _branch_model(br) or default_model
        try:
            messages = list(br.msgs.messages)
        except Exception:
            messages = []
        for m in messages:
            mr = (
                getattr(m, "metadata", {}).get("model_response") if hasattr(m, "metadata") else None
            )
            u = _dig_usage(mr) if mr else None
            if not u:
                continue
            uncached, cached, out, is_codex = _norm_tokens(u)
            if uncached or cached or out:
                usage._add(model_name, uncached, cached, out)
                usage.n_calls += 1
                usage.num_turns += int(mr.get("num_turns", 0) or 0)
                if is_codex:
                    usage.reasoning_disclosed = False
                reported_any = True

    if not reported_any:
        # Estimate fallback: tokenize prompt + final output. UNDERCOUNTS CLI
        # internal turns AND reasoning — flagged via source="estimated".
        usage.reasoning_disclosed = False
        for prompt, output in prompts_and_outputs:
            inp = TokenCalculator.calculate_message_tokens(
                [{"role": "user", "content": prompt or ""}], model=default_model
            )
            out = TokenCalculator.tokenize(output or "", encoding_name=default_model)
            usage._add(default_model, inp, 0, out)
            usage.n_calls += 1
        estimated_any = True

    usage.source = (
        "reported"
        if reported_any and not estimated_any
        else "estimated"
        if estimated_any and not reported_any
        else "mixed"
        if reported_any
        else "none"
    )
    return usage


def _branch_model(br) -> str | None:
    """Best-effort: the model name a branch's chat iModel resolves to."""
    try:
        im = br.chat_model
        for attr in ("model", "model_name"):
            v = getattr(im, attr, None)
            if isinstance(v, str):
                return v
        cfg = getattr(getattr(im, "endpoint", None), "config", None)
        v = getattr(cfg, "model", None) if cfg else None
        return v if isinstance(v, str) else None
    except Exception:
        return None
