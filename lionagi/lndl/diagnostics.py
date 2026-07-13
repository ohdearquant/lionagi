# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL diagnostics — opt-in telemetry for LNDL parse rounds. Pass a
``LndlTrace()`` via ``trace=``; default ``trace=None`` means zero overhead."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

__all__ = (
    "LndlChunkHealth",
    "LndlRoundRecord",
    "LndlTrace",
    "classify_chunk",
    "classify_result",
    "extract_lndl_chunks",
)


# Syntax classification — does an assistant chunk look like valid LNDL?


@dataclass(frozen=True)
class LndlChunkHealth:
    """Syntactic shape of a single assistant LNDL response — a lexer-free,
    structure-only check; the full parser produces semantic verdicts."""

    text: str
    has_out: bool
    open_tags: int  # count of '<l' (lact / lvar opens)
    close_tags: int  # count of '</l' (lact / lvar closes)
    balanced: bool

    @property
    def status(self) -> str:
        """One-word health: ``clean`` | ``malformed`` | ``no_out``."""
        if not self.has_out:
            return "no_out"
        if not self.balanced:
            return "malformed"
        return "clean"


def classify_chunk(text: str) -> LndlChunkHealth:
    """Classify the syntactic health of a raw LNDL chunk. Heuristic — does
    NOT parse; use ``Lexer`` + ``Parser`` for definitive syntax verdicts."""
    if not isinstance(text, str):
        return LndlChunkHealth(text="", has_out=False, open_tags=0, close_tags=0, balanced=False)
    has_out = "OUT{" in text or "OUT [" in text
    open_tags = text.count("<l")
    close_tags = text.count("</l")
    # Allow a 1-tag drift (final OUT or stray newline often unbalances by 1).
    balanced = abs(open_tags - close_tags) <= 1
    return LndlChunkHealth(
        text=text,
        has_out=has_out,
        open_tags=open_tags,
        close_tags=close_tags,
        balanced=balanced,
    )


def classify_result(value: Any) -> str:
    """Classify an operate-with-LNDL result: ``ok`` (parsed BaseModel),
    ``str`` (raw fallback), ``dict`` (validation failed), or ``empty``."""
    if value is None:
        return "empty"
    if isinstance(value, str):
        return "empty" if not value.strip() else "str"
    if isinstance(value, dict):
        if not value or all(v is None for v in value.values()):
            return "empty"
        return "dict"
    if isinstance(value, BaseModel):
        return "ok"
    return "dict"


# Per-round trace records


@dataclass
class LndlRoundRecord:
    """One LNDL parse attempt — emitted per round in multi-round mode and
    per retry in single-round mode."""

    raw: str
    outcome: str  # "success" | "continue" | "retry" | "failed" | "exhausted"
    error: str | None = None
    actions_executed: int = 0
    schema: str | None = None  # response_format class name, if known

    @property
    def health(self) -> LndlChunkHealth:
        return classify_chunk(self.raw)


@dataclass
class LndlTrace:
    """Opt-in trace container: pass via ``trace=`` and the framework appends
    one ``LndlRoundRecord`` per LNDL parse attempt (incl. retries/continuations)."""

    rounds: list[LndlRoundRecord] = field(default_factory=list)

    def append(self, record: LndlRoundRecord) -> None:
        self.rounds.append(record)

    @property
    def chunks(self) -> list[str]:
        return [r.raw for r in self.rounds if r.raw]

    def health(self) -> dict[str, int]:
        """Aggregate syntactic health across all chunks."""
        out = {"clean": 0, "malformed": 0, "no_out": 0}
        for r in self.rounds:
            if r.raw:
                out[r.health.status] += 1
        return out

    def outcomes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rounds:
            out[r.outcome] = out.get(r.outcome, 0) + 1
        return out

    def errors(self) -> list[tuple[int, str]]:
        return [(i, r.error) for i, r in enumerate(self.rounds) if r.error is not None]

    def summary(self) -> str:
        h = self.health()
        o = self.outcomes()
        clean_pct = 100 * h["clean"] / max(1, sum(h.values())) if sum(h.values()) else 0
        return (
            f"LndlTrace({len(self.rounds)} rounds | "
            f"health={h} ({clean_pct:.0f}% clean) | "
            f"outcomes={o})"
        )

    def __len__(self) -> int:
        return len(self.rounds)


# Public utility — extract LNDL strings from a Branch's message log


def extract_lndl_chunks(messages: Any, since: int = 0) -> list[str]:
    """Pull raw LNDL strings (matching ``<lact``, ``<lvar``, ``OUT{``, ``OUT [``)
    from assistant messages added after ``since``, for callers that skipped ``trace=``."""
    chunks: list[str] = []
    msgs = list(messages)
    for i in range(since, len(msgs)):
        msg = msgs[i]
        body = getattr(getattr(msg, "content", None), "assistant_response", None)
        if not body:
            continue
        text = str(body)
        if any(tok in text for tok in ("<lact", "<lvar", "OUT{", "OUT [")):
            chunks.append(text)
    return chunks
