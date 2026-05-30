# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Provider-specific permission adapters.

Translates a PermissionPolicy into endpoint kwargs understood by a CLI provider.
v1 ships one adapter: claude_code.  Other providers (codex, openai) are future work.
"""

from __future__ import annotations

from .claude_code import translate_permissions

__all__ = ("translate_permissions",)
