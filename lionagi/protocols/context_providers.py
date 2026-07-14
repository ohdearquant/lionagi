# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pre-turn context injection: renders ephemeral knowledge into the
first-message guidance fold, never the durable message record. See ADR-0008."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch

__all__ = (
    "ContextProvider",
    "ProviderReport",
    "ContextProviderRegistry",
)

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET = 2000


@runtime_checkable
class ContextProvider(Protocol):
    """Structural contract for pre-turn knowledge injection."""

    async def provide(self, branch: Branch, instruction: Instruction) -> str | None: ...


@dataclass(frozen=True)
class ProviderReport:
    """Per-turn observability: rendered blocks plus which providers fired,
    were skipped (budget) or failed (exception or invalid output)."""

    blocks: list[str] = field(default_factory=list)
    fired: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class _Entry:
    provider: ContextProvider
    priority: int
    max_tokens: int | None
    name: str


class ContextProviderRegistry:
    """Ordered ContextProviders under a total injection budget; drops
    lowest-priority providers first over budget, warns + skips on error."""

    def __init__(self, budget: int = _DEFAULT_BUDGET):
        self.budget = budget
        self._entries: list[_Entry] = []

    def register(
        self,
        provider: ContextProvider,
        *,
        priority: int = 0,
        max_tokens: int | None = None,
        name: str | None = None,
    ) -> None:
        name = name or getattr(provider, "name", None) or type(provider).__name__
        self._entries.append(
            _Entry(provider=provider, priority=priority, max_tokens=max_tokens, name=name)
        )

    def __bool__(self) -> bool:
        return bool(self._entries)

    @property
    def names(self) -> list[str]:
        return [entry.name for entry in self._entries]

    def __len__(self) -> int:
        return len(self._entries)

    async def gather(self, branch: Branch, instruction: Instruction) -> ProviderReport:
        report = ProviderReport()
        if not self._entries:
            return report

        from lionagi.service.token_calculator import TokenCalculator

        successes: list[tuple[_Entry, str, int]] = []
        for entry in self._entries:
            try:
                text = await entry.provider.provide(branch, instruction)
                if text is not None and not isinstance(text, str):
                    raise TypeError("context provider output must be a string or None")
                if not text:
                    continue
                tokens = TokenCalculator.tokenize(text)
                if entry.max_tokens and tokens > entry.max_tokens:
                    report.skipped.append(entry.name)
                    continue
                successes.append((entry, text, tokens))
            except Exception:
                logger.warning("context provider %r raised; skipping", entry.name, exc_info=True)
                report.failed.append(entry.name)

        # Drop lowest-priority first over budget; stable sort preserves
        # registration order among equal priorities.
        by_priority = sorted(successes, key=lambda item: item[0].priority, reverse=True)

        kept_ids: set[int] = set()
        total = 0
        for entry, _text, tokens in by_priority:
            if total + tokens > self.budget:
                report.skipped.append(entry.name)
                continue
            total += tokens
            kept_ids.add(id(entry))

        for entry, text, tokens in successes:
            if id(entry) in kept_ids:
                report.blocks.append(text)
                report.fired.append({"provider_name": entry.name, "tokens": tokens})

        return report

    async def gather_writeback(self, branch: Branch, action_responses: list) -> None:
        """POST-turn hook: providers with an optional `writeback` method persist
        from the turn's action responses; errors are warned + skipped, never block."""
        for entry in self._entries:
            hook = getattr(entry.provider, "writeback", None)
            if hook is None:
                continue
            try:
                await hook(branch, action_responses)
            except Exception:
                logger.warning(
                    "context provider %r writeback raised; skipping", entry.name, exc_info=True
                )
