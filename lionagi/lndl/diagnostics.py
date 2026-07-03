# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL diagnostics — opt-in telemetry for LNDL parse rounds.

These primitives let callers introspect what the model actually emitted
during ``branch.operate(lndl=True, ...)`` or ``branch.ReActStream(lndl=True, ...)``:

    from lionagi.lndl import LndlTrace

    trace = LndlTrace()
    result = await branch.operate(
        instruction="...",
        response_format=Report,
        lndl=True,
        trace=trace,
    )
    print(trace.summary())          # quick health snapshot
    for r in trace.rounds:
        print(r.outcome, r.error)   # per-round details

The trace is **opt-in** — callers must instantiate ``LndlTrace`` and pass it.
Default behaviour and return types are unchanged: ``trace=None`` (default)
means zero overhead.

Two layers of classification:

* **Syntax** (``classify_chunk``): does this raw assistant text look like
  well-formed LNDL? ``clean`` / ``malformed`` / ``no_out``.
* **Outcome** (``LndlRoundRecord.outcome``): what did the framework decide
  this round? ``success`` / ``continue`` / ``retry`` / ``failed`` /
  ``exhausted`` — mirrors the ``RoundOutcome`` ADT.
* **Result** (``classify_result``): for a final operate return value, what
  did we get? ``ok`` (parsed BaseModel) / ``str`` (raw fallback) /
  ``dict`` (validation failed) / ``empty`` (None or empty container).

These three views answer different questions: *did the model write valid
LNDL?* / *what did the framework do with it?* / *what did the user get?*
"""

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


# ---------------------------------------------------------------------------
# Syntax classification — does an assistant chunk look like valid LNDL?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LndlChunkHealth:
    """Syntactic shape of a single assistant LNDL response.

    This is a lexer-free, structure-only check intended to answer the
    question "is this chunk in the LNDL ballpark?" without invoking the
    full parser. The full parser is what produces semantic verdicts.
    """

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
    """Classify the syntactic health of a raw LNDL chunk.

    Heuristic — does NOT parse the chunk. Intended for fast pre-screening
    and aggregate health metrics. Use ``Lexer`` + ``Parser`` for definitive
    syntax verdicts.
    """
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
    """Classify what an operate-with-LNDL result actually is.

    Returns one of:

    * ``ok``    — a parsed ``BaseModel`` (the schema-shaped return).
    * ``str``   — raw LNDL fallback (parse or assembly failure leaked through).
    * ``dict``  — partial dict (validation failed but the framework kept
      the unstructured value rather than raising).
    * ``empty`` — ``None``, empty string, empty dict, or all-``None`` dict.
    """
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


# ---------------------------------------------------------------------------
# Per-round trace records
# ---------------------------------------------------------------------------


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
    """Opt-in trace container for LNDL operate / ReActStream calls.

    Pass an instance via ``trace=`` and the framework will append one
    ``LndlRoundRecord`` per LNDL parse attempt (including retries and
    multi-round continuations).
    """

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


# ---------------------------------------------------------------------------
# Public utility — extract LNDL strings from a Branch's message log
# ---------------------------------------------------------------------------


def extract_lndl_chunks(messages: Any, since: int = 0) -> list[str]:
    """Pull raw LNDL strings from assistant messages added after ``since``.

    Filters to assistant messages whose content contains LNDL syntax
    markers (``<lact``, ``<lvar``, ``OUT{``, ``OUT [``). Useful when you
    didn't pass a ``trace=`` (or want chunks from non-LNDL messages too).

    Args:
        messages: An iterable of messages — typically ``branch.messages``.
        since: Start index. Pass ``len(branch.messages)`` before a call,
            then call again after to get chunks emitted by that call only.
    """
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
