# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Exceptions shared by MCP submission admission and configuration reading."""

from __future__ import annotations

__all__ = ("SubmitInputError",)


class SubmitInputError(ValueError):
    """A submission refused because the caller's input cannot be honoured."""
