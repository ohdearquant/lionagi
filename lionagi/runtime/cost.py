# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LLM API call cost tracking for lionagi.

Provides a simple, thread-safe cost ledger that can accumulate monetary
costs across model calls, enforce budget limits, and produce per-model
breakdowns.  All cost values are in US dollars (float) at the Python
layer; callers that need cent-precision for persistence should multiply
by 100 and round to the nearest integer.

Public API:
    BudgetExceededError   – raised when recorded cost exceeds a budget cap
    CostEntry             – immutable record of one model call's cost
    PricingTable          – dict-based pricing lookup with built-in defaults
    CostLedger            – thread-safe accumulator with budget enforcement
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = (
    "BudgetExceededError",
    "CostEntry",
    "CostLedger",
    "PricingTable",
)

# ---------------------------------------------------------------------------
# Default per-model pricing (USD per 1 000 tokens)
# Rates are approximate public list prices; callers may override via
# PricingTable.register_rate() or by passing a custom rates dict.
# ---------------------------------------------------------------------------
_DEFAULT_RATES: dict[str, tuple[float, float]] = {
    # (input_per_1k_usd, output_per_1k_usd)
    # OpenAI
    "gpt-4.1-mini": (0.000400, 0.001600),
    "gpt-4.1-mini-2025-04-14": (0.000400, 0.001600),
    "gpt-4.1": (0.002000, 0.008000),
    "gpt-4.1-2025-04-14": (0.002000, 0.008000),
    "gpt-4o": (0.002500, 0.010000),
    "gpt-4o-mini": (0.000150, 0.000600),
    "gpt-4-turbo": (0.010000, 0.030000),
    "o1": (0.015000, 0.060000),
    "o1-mini": (0.003000, 0.012000),
    "o3-mini": (0.001100, 0.004400),
    # Anthropic
    "claude-sonnet-4-5-20250514": (0.003000, 0.015000),
    "claude-sonnet-4-5": (0.003000, 0.015000),
    "claude-opus-4-20250514": (0.015000, 0.075000),
    "claude-opus-4": (0.015000, 0.075000),
    "claude-haiku-3-5-20241022": (0.000800, 0.004000),
    "claude-haiku-3-5": (0.000800, 0.004000),
    "claude-3-5-sonnet-20241022": (0.003000, 0.015000),
    "claude-3-5-haiku-20241022": (0.000800, 0.004000),
    # Google
    "gemini-2.0-flash": (0.000100, 0.000400),
    "gemini-1.5-pro": (0.001250, 0.005000),
    "gemini-1.5-flash": (0.000075, 0.000300),
}


# ---------------------------------------------------------------------------
# BudgetExceededError
# ---------------------------------------------------------------------------


class BudgetExceededError(Exception):
    """Raised when a :class:`CostLedger` exceeds its configured budget cap.

    Attributes:
        budget_usd:    The configured budget in US dollars.
        total_cost:    The actual accumulated cost at the time of the error.
    """

    def __init__(self, budget_usd: float, total_cost: float) -> None:
        self.budget_usd = budget_usd
        self.total_cost = total_cost
        super().__init__(f"Budget exceeded: ${total_cost:.6f} spent, ${budget_usd:.6f} limit")


# ---------------------------------------------------------------------------
# CostEntry
# ---------------------------------------------------------------------------


class CostEntry(BaseModel):
    """Immutable record capturing the cost of a single model API call.

    All cost values are in US dollars.

    Attributes:
        entry_id:       Unique identifier for this entry (UUID hex string).
        model_id:       Model name as returned by the provider.
        provider:       Provider identifier (e.g. "openai", "anthropic").
        input_tokens:   Number of input/prompt tokens consumed.
        output_tokens:  Number of output/completion tokens generated.
        total_tokens:   Sum of input and output tokens.
        cost_usd:       Total cost in US dollars.
        timestamp:      Unix timestamp (seconds since epoch) of the call.
        operation_id:   Optional identifier for the enclosing operation/step.
        session_id:     Optional session/run identifier.
        metadata:       Optional free-form operational metadata.
    """

    model_config = ConfigDict(frozen=True)

    entry_id: str
    model_id: str
    provider: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    timestamp: float
    operation_id: str | None
    session_id: str | None
    metadata: dict[str, Any] | None


# ---------------------------------------------------------------------------
# PricingTable
# ---------------------------------------------------------------------------


class PricingTable:
    """Dictionary-based lookup for model pricing rates.

    Rates are expressed as USD per 1 000 tokens.

    Args:
        rates: Optional mapping of ``{model_id: (input_per_1k, output_per_1k)}``.
               When omitted the built-in defaults (:data:`_DEFAULT_RATES`) are
               used as the initial rate table.

    Example::

        table = PricingTable()
        cost = table.compute_cost("gpt-4.1-mini", 500, 300)

        table2 = PricingTable({"my-model": (0.001, 0.002)})
        cost2 = table2.compute_cost("my-model", 1000, 500)
    """

    def __init__(self, rates: dict[str, tuple[float, float]] | None = None) -> None:
        if rates is None:
            self._rates: dict[str, tuple[float, float]] = dict(_DEFAULT_RATES)
        else:
            self._rates = dict(rates)

    def compute_cost(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        """Compute USD cost for a model call.

        Args:
            model_id:      The model identifier.
            input_tokens:  Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            Total cost in US dollars.

        Raises:
            KeyError: If ``model_id`` is not registered in this table.
        """
        input_per_1k, output_per_1k = self._rates[model_id]
        return (input_tokens * input_per_1k + output_tokens * output_per_1k) / 1000.0

    def register_rate(
        self,
        model_id: str,
        input_per_1k: float,
        output_per_1k: float,
    ) -> None:
        """Add or update the pricing for a model.

        Args:
            model_id:       The model identifier to register.
            input_per_1k:   USD cost per 1 000 input tokens.
            output_per_1k:  USD cost per 1 000 output tokens.
        """
        self._rates[model_id] = (input_per_1k, output_per_1k)

    def known_models(self) -> list[str]:
        """Return a sorted list of all registered model identifiers."""
        return sorted(self._rates)


# ---------------------------------------------------------------------------
# CostLedger
# ---------------------------------------------------------------------------


class CostLedger:
    """Thread-safe accumulator for LLM API call costs.

    Records individual :class:`CostEntry` objects, maintains running totals,
    and optionally enforces a hard USD budget cap.

    Args:
        budget_usd: Maximum total spend in USD.  When the cumulative cost of
                    recorded entries exceeds this value a
                    :class:`BudgetExceededError` is raised on the next
                    :meth:`record` call that pushes over the limit.
        pricing:    :class:`PricingTable` used to compute per-call costs.
                    When omitted a table seeded with built-in defaults is used.

    Example::

        ledger = CostLedger(budget_usd=1.0)
        entry = ledger.record("gpt-4.1-mini", 500, 300, provider="openai")
        print(ledger.total_cost())   # → float (USD)
        print(ledger.summary())      # → dict with breakdown by model
    """

    def __init__(
        self,
        budget_usd: float | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        self._budget_usd = budget_usd
        self._pricing: PricingTable = pricing if pricing is not None else PricingTable()
        self._entries: list[CostEntry] = []
        self._total_cost: float = 0.0
        self._total_tokens: int = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public mutating method
    # ------------------------------------------------------------------

    def record(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        *,
        provider: str = "",
        operation_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CostEntry:
        """Record one model API call and return the resulting :class:`CostEntry`.

        The cost is computed from the :class:`PricingTable`, added to the
        running totals, and checked against the budget (if configured).

        Args:
            model_id:      Model identifier (must be in the pricing table).
            input_tokens:  Input token count for this call.
            output_tokens: Output token count for this call.
            provider:      Optional provider label (e.g. "openai").
            operation_id:  Optional operation/step identifier.
            session_id:    Optional session identifier.
            metadata:      Optional free-form metadata dict.

        Returns:
            The newly created :class:`CostEntry`.

        Raises:
            KeyError:             If ``model_id`` is not in the pricing table.
            BudgetExceededError:  If recording this entry causes total cost to
                                  exceed the configured ``budget_usd``.
        """
        cost = self._pricing.compute_cost(model_id, input_tokens, output_tokens)
        total_tokens = input_tokens + output_tokens
        entry = CostEntry(
            entry_id=uuid.uuid4().hex,
            model_id=model_id,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            timestamp=time.time(),
            operation_id=operation_id,
            session_id=session_id,
            metadata=metadata,
        )

        with self._lock:
            self._entries.append(entry)
            self._total_cost += cost
            self._total_tokens += total_tokens
            if self._budget_usd is not None and self._total_cost > self._budget_usd:
                raise BudgetExceededError(self._budget_usd, self._total_cost)

        return entry

    # ------------------------------------------------------------------
    # Public read-only accessors
    # ------------------------------------------------------------------

    def total_cost(self) -> float:
        """Return the accumulated cost in US dollars."""
        with self._lock:
            return self._total_cost

    def total_tokens(self) -> int:
        """Return the total token count across all recorded entries."""
        with self._lock:
            return self._total_tokens

    def entries(
        self,
        model_id: str | None = None,
        session_id: str | None = None,
    ) -> list[CostEntry]:
        """Return a filtered list of recorded :class:`CostEntry` objects.

        Args:
            model_id:   When given, include only entries for this model.
            session_id: When given, include only entries with this session ID.

        Returns:
            A new list containing the matching entries in insertion order.
        """
        with self._lock:
            result = list(self._entries)

        if model_id is not None:
            result = [e for e in result if e.model_id == model_id]
        if session_id is not None:
            result = [e for e in result if e.session_id == session_id]
        return result

    def summary(self) -> dict[str, Any]:
        """Return a summary dict with totals and a per-model breakdown.

        Returns a dict with the following structure::

            {
                "total_cost":   float,   # total USD
                "total_tokens": int,     # total tokens
                "by_model": {
                    "<model_id>": {
                        "cost":   float,
                        "tokens": int,
                        "calls":  int,
                    },
                    ...
                },
            }
        """
        with self._lock:
            snapshot = list(self._entries)
            total_cost = self._total_cost
            total_tokens = self._total_tokens

        by_model: dict[str, dict[str, Any]] = {}
        for entry in snapshot:
            bucket = by_model.setdefault(entry.model_id, {"cost": 0.0, "tokens": 0, "calls": 0})
            bucket["cost"] += entry.cost_usd
            bucket["tokens"] += entry.total_tokens
            bucket["calls"] += 1

        return {
            "total_cost": total_cost,
            "total_tokens": total_tokens,
            "by_model": by_model,
        }

    def remaining_budget(self) -> float | None:
        """Return the remaining budget in USD, or ``None`` if no budget is set.

        The returned value may be negative if the ledger is already over budget
        (which would also have raised :class:`BudgetExceededError` at record
        time, so callers that catch that error may see a negative remainder when
        inspecting the ledger afterward).
        """
        if self._budget_usd is None:
            return None
        with self._lock:
            return self._budget_usd - self._total_cost

    def is_over_budget(self) -> bool:
        """Return ``True`` if the total cost exceeds the configured budget."""
        if self._budget_usd is None:
            return False
        with self._lock:
            return self._total_cost > self._budget_usd
