# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounds shared by every surface that validates an orchestration spec.

Deliberately importing nothing — see docs/internals/support-libs.md#_spec_limits-max_spec_prompt_chars.
"""

from __future__ import annotations

# See docs/internals/support-libs.md#_spec_limits-max_spec_prompt_chars
MAX_SPEC_PROMPT_CHARS = 256 * 1024
