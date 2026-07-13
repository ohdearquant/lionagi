# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``ScriptModel`` — parsed, matchable test fixture: positional + ``when:`` conditional response entries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from ._types import (
    ErrorResponse,
    ResponseEntry,
    StreamResponse,
    StructuredResponse,
    TextResponse,
    ToolCallResponse,
    WhenMatcher,
)


def _build_entry(data: dict[str, Any]) -> ResponseEntry:
    """Construct a ``ResponseEntry`` from a dict, dispatching to the subclass by ``type``
    (manual dispatch, not a pydantic discriminated union — see docs/internals/runtime.md)."""

    if not isinstance(data, dict):
        raise TypeError(f"response entry must be dict, got {type(data).__name__}")
    t = data.get("type")
    cls_map: dict[str, type[ResponseEntry]] = {
        "text": TextResponse,
        "tool_call": ToolCallResponse,
        "structured": StructuredResponse,
        "stream": StreamResponse,
        "error": ErrorResponse,
    }
    if t not in cls_map:
        raise ValueError(f"unknown response type {t!r}; expected one of {sorted(cls_map)}")
    return cls_map[t].model_validate(data)


class ScriptModel(BaseModel):
    """Parsed test script — load via ``from_yaml``, ``from_json``, ``from_responses``, or ``coerce``."""

    model_config = {"extra": "forbid"}

    version: int = 1
    mode: str = Field(default="auto", pattern="^(auto|positional|when_only)$")
    responses: list[ResponseEntry] = Field(default_factory=list)

    # ── runtime cursors — PrivateAttr so they don't appear in model_dump
    _cursor: int = PrivateAttr(default=0)
    _served_by_when: list[int] = PrivateAttr(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_responses(cls, data: Any) -> Any:
        """Accept raw dicts in ``responses`` and dispatch to subclasses; works on a shallow
        copy so the caller-supplied dict is never mutated."""

        if isinstance(data, dict):
            data = dict(data)
            raw = data.get("responses")
            if isinstance(raw, list):
                coerced: list[Any] = []
                for entry in raw:
                    if isinstance(entry, dict):
                        coerced.append(_build_entry(dict(entry)))
                    else:
                        coerced.append(entry)
                data["responses"] = coerced
        return data

    # ─────────────────────────────── factories ────────────────────────────

    @classmethod
    def coerce(cls, source: Any) -> ScriptModel:
        """Best-effort load from a ScriptModel, str/Path (file), dict (script shape), or
        list (raw responses)."""
        if isinstance(source, cls):
            return source.model_copy(deep=True)
        if isinstance(source, str | Path):
            p = Path(source)
            if p.suffix in {".yaml", ".yml"}:
                return cls.from_yaml(p)
            return cls.from_json(p)
        if isinstance(source, list):
            return cls.from_responses(source)
        if isinstance(source, dict):
            return cls.model_validate(source)
        raise TypeError(
            f"cannot coerce {type(source).__name__} to ScriptModel; "
            "pass a path, dict, list, or ScriptModel instance"
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> ScriptModel:
        """Load a YAML script."""
        import yaml

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data or {})

    @classmethod
    def from_json(cls, path: str | Path) -> ScriptModel:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data or {})

    @classmethod
    def from_responses(
        cls, responses: list[dict[str, Any] | ResponseEntry], **kwargs: Any
    ) -> ScriptModel:
        """Build from a flat list of response dicts (no version/mode wrapping)."""
        return cls.model_validate({"responses": responses, **kwargs})

    # ─────────────────────────────── matching ─────────────────────────────

    def next(self, payload: dict[str, Any], call_index: int) -> tuple[ResponseEntry, str]:
        """Pick the next response: try ``when:`` matchers first (unless mode is
        ``positional``), then fall back to positional order — see docs/internals/runtime.md."""

        # Phase 1: try ``when:`` matchers (skipped only when mode == positional).
        if self.mode != "positional":
            for i, entry in enumerate(self.responses):
                if entry.when is None or entry.when.is_empty():
                    continue
                if i in self._served_by_when:
                    continue
                ok, why = self._matches(entry.when, payload, call_index)
                if ok:
                    self._served_by_when.append(i)
                    return entry, why

        if self.mode == "when_only":
            raise ScriptExhaustedError(
                f"no when: matcher matched call_index={call_index}; "
                f"payload last user message={_extract_last_user(payload)!r}"
            )

        # Phase 2: positional fallback. Skip entries with non-empty when:
        # (they're reserved for matching).
        while self._cursor < len(self.responses):
            entry = self.responses[self._cursor]
            self._cursor += 1
            if entry.when is not None and not entry.when.is_empty():
                continue
            return entry, "positional"

        positional_count = sum(1 for e in self.responses if e.when is None or e.when.is_empty())
        raise ScriptExhaustedError(
            f"script exhausted at call_index={call_index}; "
            f"only {positional_count} positional entries available; "
            f"payload last user message={_extract_last_user(payload)!r}"
        )

    @staticmethod
    def _matches(when: WhenMatcher, payload: dict[str, Any], call_index: int) -> tuple[bool, str]:
        if when.call_index is not None and call_index != when.call_index:
            return False, ""
        if when.after_calls is not None and call_index < when.after_calls:
            return False, ""

        last_user = _extract_last_user(payload) or ""
        if when.prompt_contains is not None:
            if when.prompt_contains.lower() not in last_user.lower():
                return False, ""
            return True, f"when:prompt_contains:{when.prompt_contains!r}"
        if when.prompt_regex is not None:
            if not re.search(when.prompt_regex, last_user):
                return False, ""
            return True, f"when:prompt_regex:{when.prompt_regex!r}"
        if when.has_tool is not None:
            tools = payload.get("tools") or []
            names = [
                ((t.get("function") or {}).get("name") or t.get("name"))
                for t in tools
                if isinstance(t, dict)
            ]
            if when.has_tool not in names:
                return False, ""
            return True, f"when:has_tool:{when.has_tool!r}"

        # No content predicate but call_index / after_calls matched — that's
        # enough to count as a match.
        if when.call_index is not None:
            return True, f"when:call_index:{when.call_index}"
        if when.after_calls is not None:
            return True, f"when:after_calls:{when.after_calls}"
        return False, ""

    # ─────────────────────────────── inspection ───────────────────────────

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def exhausted(self) -> bool:
        if self.mode == "when_only":
            return len(self._served_by_when) == sum(1 for e in self.responses if e.when is not None)
        # Positional cursor past the end AND no remaining positional entries.
        for i in range(self._cursor, len(self.responses)):
            e = self.responses[i]
            if e.when is None or e.when.is_empty():
                return False
        return True

    def reset(self) -> None:
        self._cursor = 0
        self._served_by_when = []


class ScriptExhaustedError(RuntimeError):
    """Raised when no response is available for a call — extend the script or add a ``when:`` entry."""


def _extract_last_user(payload: dict[str, Any]) -> str | None:
    msgs = payload.get("messages") or []
    for msg in reversed(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                return "".join(parts) or None
    # OpenAI Responses API: ``input`` field
    inp = payload.get("input")
    if isinstance(inp, str):
        return inp
    return None


__all__ = ("ScriptExhaustedError", "ScriptModel")
