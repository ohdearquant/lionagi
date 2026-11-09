# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Compatibility entry point for row-bound quarantine identity."""

from typing import Any

from lionagi.state._quarantine_identity import quarantine_identity as _quarantine_identity


def quarantine_identity(row: dict[str, Any]) -> tuple[int, float | None, str | None] | None:
    """Return the shared canonical row-bound process identity."""
    return _quarantine_identity(row)
