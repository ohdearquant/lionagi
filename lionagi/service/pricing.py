"""Static list-price table for engines that report tokens but not cost.

The Claude CLI reports its own ``total_cost_usd`` per turn; the Codex CLI
(and every OpenRouter-served model behind it) reports token counts only. For
those branches the usage writer estimates cost from this table so a run's
cost column reflects every leg, not only the engines that price themselves.

Prices are USD per million tokens (input, output), taken from the provider's
public price list. An estimate is exactly that: a model missing from this
table yields ``None`` and the branch keeps cost NULL (unreported) rather
than a fabricated zero. Subscription-served models (e.g. ``gpt-5.6`` via a
seat plan) have no marginal per-token price and are deliberately absent.
"""

from __future__ import annotations

# Substring key (matched case-insensitively against the resolved model spec)
# -> (usd_per_million_input_tokens, usd_per_million_output_tokens).
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "kimi-k3": (3.00, 15.00),
    "glm-5.2": (0.76, 2.42),
    "minimax-m3": (0.30, 1.20),
    "qwen3.8-max": (2.00, 6.00),
    "deepseek-v4-flash": (0.09, 0.18),
}


def estimate_cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated USD cost for a token count, or None when unpriceable.

    Longest matching substring wins so a more specific entry can shadow a
    family entry. Zero tokens is not evidence of a free call — it usually
    means usage never arrived — so it also returns None.
    """
    if not model or (input_tokens <= 0 and output_tokens <= 0):
        return None
    spec = model.lower()
    best: tuple[float, float] | None = None
    best_len = 0
    for key, prices in MODEL_PRICES.items():
        if key in spec and len(key) > best_len:
            best = prices
            best_len = len(key)
    if best is None:
        return None
    in_price, out_price = best
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000
