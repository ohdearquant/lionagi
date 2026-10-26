# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read global settings and the nearest project settings without wiring agent hooks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lionagi._paths import is_account_lionagi_dir
from lionagi.libs.nested import deep_merge as _deep_merge_impl

__all__ = ("load_settings",)


def load_settings(
    project_dir: str | Path | None = None,
    *,
    include_project: bool = True,
) -> dict[str, Any]:
    """Load and merge ~/.lionagi/settings.yaml with project-local override; auto-detects cwd."""
    merged: dict[str, Any] = {}

    global_path = Path.home() / ".lionagi" / "settings.yaml"
    if global_path.is_file():
        with open(global_path) as f:
            global_settings = yaml.safe_load(f) or {}
        _deep_merge(merged, global_settings)

    if not include_project:
        return merged

    project_root = Path(project_dir) if project_dir else Path.cwd()
    for parent in [project_root, *project_root.parents]:
        candidate = parent / ".lionagi" / "settings.yaml"
        if candidate.is_file():
            # The account home's own file is the global layer, read above through
            # HOME or not at all. As the nearest file it still ends the search, so
            # excluding it never promotes a farther one.
            if not is_account_lionagi_dir(candidate.parent):
                with open(candidate) as f:
                    local_settings = yaml.safe_load(f) or {}
                _deep_merge(merged, local_settings)
            break

    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    return _deep_merge_impl(base, override, mutate=True)
