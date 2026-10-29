# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read global settings and the nearest project settings without wiring agent hooks."""

from __future__ import annotations

from pathlib import Path
from stat import S_ISLNK, S_ISREG
from typing import Any

import yaml

from lionagi._paths import is_account_lionagi_dir
from lionagi.libs.nested import deep_merge as _deep_merge_impl

__all__ = ("load_global_settings_layer", "load_project_settings_layer", "load_settings")


def _layer_entry_present(path: Path) -> bool:
    """Treat a broken settings symlink as an error, not absent configuration."""
    try:
        path.lstat()
    except NotADirectoryError:
        # An ordinary file at this project's .lionagi entry cannot contain a
        # settings layer. A symlink to a file is an explicit broken directory
        # reference and remains a read failure instead of selecting an ancestor.
        if S_ISREG(path.parent.lstat().st_mode):
            return False
        raise
    except FileNotFoundError:
        # A broken symlink at the settings directory also makes its child
        # appear absent. Check that entry without resolving any higher ancestor.
        try:
            parent = path.parent.lstat()
        except FileNotFoundError:
            return False
        if S_ISLNK(parent.st_mode):
            path.parent.stat()  # A dangling target must remain a read failure.
        return False
    return True


def _read_layer(path: Path) -> Any:
    try:
        with path.open() as stream:
            source = stream.read()
    except FileNotFoundError:
        if _layer_entry_present(path):
            raise
        return {}
    # An empty or comment-only document has no node. An explicit YAML null is
    # a real (invalid for settings) scalar and must not become chosen silence.
    node = yaml.compose(source, Loader=yaml.SafeLoader)
    if node is None or (
        node.tag == "tag:yaml.org,2002:null"
        and node.value == ""
        and node.start_mark.index == node.end_mark.index
    ):
        return {}
    return yaml.safe_load(source)


def load_global_settings_layer() -> Any:
    """Read the raw global layer without merging or hiding malformed roots."""
    return _read_layer(Path.home() / ".lionagi" / "settings.yaml")


def load_project_settings_layer(project_dir: str | Path | None = None) -> Any:
    """Read the nearest project layer without merging it with global settings."""
    root = Path(project_dir) if project_dir else Path.cwd()
    for parent in (root, *root.parents):
        candidate = parent / ".lionagi" / "settings.yaml"
        if _layer_entry_present(candidate) and (candidate.is_file() or candidate.is_symlink()):
            if is_account_lionagi_dir(candidate.parent):
                return {}
            return _read_layer(candidate)
    return {}


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
