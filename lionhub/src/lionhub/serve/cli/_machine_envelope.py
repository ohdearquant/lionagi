# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Machine result encoding available before command dispatch can load."""

from __future__ import annotations

import json
from typing import Any

# The one place the current contract version lives. Incremented only by a change
# that could break a conforming consumer — which includes any field that changes
# what an existing field means to a reader that ignores it.
_CONTRACT_VERSION = 1

# Closed, on purpose: this vocabulary describes our own refusal to answer, which
# we control, so a consumer may branch on it and a new kind costs a version
# increment. Run status is the opposite case and is not modelled here.
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
