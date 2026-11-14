# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Machine result encoding available before command dispatch can load."""

from __future__ import annotations

import json
from typing import Any

# Incremented only by a change that could break a conforming consumer.
_CONTRACT_VERSION = 1

# Run status has its own vocabulary and does not belong in this closed set.
_ERROR_KINDS = ("not_found", "invalid_input", "conflict", "unavailable", "internal")


def _failure(kind: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in _ERROR_KINDS:
        raise ValueError(f"error kind {kind!r} is not one of {_ERROR_KINDS}")
    return {
        "ok": False,
        "contract_version": _CONTRACT_VERSION,
        "data": None,
        "error": {"kind": kind, "message": message, "detail": detail},
    }


def _encode_envelope(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False) + "\n"
