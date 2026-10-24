# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Strict JSON decoding with the existing orjson input and error contract."""

from __future__ import annotations

from typing import Any

import orjson

__all__ = ("JSONDecodeError", "json_loads")

JSONDecodeError = orjson.JSONDecodeError


def json_loads(data: str | bytes | bytearray | memoryview, /) -> Any:
    """Decode one UTF-8 JSON document without repair or fallback conversion.

    Accept the same inputs and preserve the same errors as ``orjson.loads``.
    ``JSONDecodeError`` is the native class, not a translated exception.
    Use the separate fuzzy helpers when repaired model output is intended.
    """
    return orjson.loads(data)
