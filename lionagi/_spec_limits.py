# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounds shared by every surface that validates an orchestration spec.

Deliberately importing nothing — see docs/internals/support-libs.md#spec-limits.
"""

from __future__ import annotations

# See docs/internals/support-libs.md#spec-limits
MAX_SPEC_PROMPT_CHARS = 256 * 1024
