# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""argparse ``type`` callables that state the shape they accept.

A command line carries one string per flag, so a flag whose value is a list or
an object can only arrive JSON-encoded. Doing that decode inside a command
handler leaves the shape undeclared: the parser says ``string``, anything
reading the parser says ``string``, and the caller finds out that a plain
string is refused only by sending one. Declaring it as the argument's ``type``
puts the shape on the argument itself, where the parser enforces it and a
reader of the parser can describe it.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

__all__ = ("BoundedInt", "JsonArgument")


class BoundedInt:
    """A whole number in ``[minimum, maximum]``, refused by the parser outside it.

    Declared on the argument so a reader of the parser can describe the range:
    the MCP projection advertises it as ``minimum``/``maximum``, and the
    dispatcher refuses an out-of-range value before any job exists.
    """

    def __init__(self, minimum: int, maximum: int) -> None:
        self.minimum = minimum
        self.maximum = maximum
        # argparse prints `type.__name__` in its own error prose.
        self.__name__ = "int"

    def __call__(self, raw: str) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"must be a whole number, not {raw!r}") from exc
        if not self.minimum <= value <= self.maximum:
            raise argparse.ArgumentTypeError(
                f"must be from {self.minimum} to {self.maximum}, not {value}"
            )
        return value

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"BoundedInt({self.minimum}, {self.maximum})"


class JsonArgument:
    """Decode one JSON-encoded flag value and check it against *schema*.

    ``schema`` is the JSON Schema of the **decoded** value — the shape a caller
    thinks in — and is bounded to what argparse can be handed on a command
    line: an ``array`` of scalars, or an ``object``.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self.json_schema = dict(schema)
        # argparse prints `type.__name__` in its own error prose.
        self.__name__ = f"json-{self.json_schema.get('type', 'value')}"

    def __call__(self, raw: str) -> Any:
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"must be valid JSON: {exc}") from exc
        want = self.json_schema.get("type")
        if want == "array" and not isinstance(value, list):
            raise argparse.ArgumentTypeError("must be a JSON array")
        if want == "object" and not isinstance(value, dict):
            raise argparse.ArgumentTypeError("must be a JSON object")
        item = self.json_schema.get("items", {}).get("type") if want == "array" else None
        if item == "string" and not all(isinstance(e, str) for e in value):
            raise argparse.ArgumentTypeError("must be a JSON array of strings")
        return value

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"JsonArgument({self.json_schema!r})"
