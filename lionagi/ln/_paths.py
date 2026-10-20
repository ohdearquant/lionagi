# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private path normalization shared by existing tool constructors."""

from __future__ import annotations

from pathlib import Path


def _resolve_workspace_root(workspace_root: str | Path | None = None) -> Path:
    """Normalize a root, resolving a missing or empty root from the current cwd."""
    return Path(workspace_root or Path.cwd()).expanduser().resolve()
