# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Locate source files for repository-relative tests."""

from __future__ import annotations

from pathlib import Path

import toml


def lionagi_pyproject(start: Path, top: Path | None = None) -> Path | None:
    """Return the lionagi pyproject.toml above ``start``.

    If no matching project is found, raise inside a git checkout and return
    None outside one.
    """
    parents = [d for d in start.parents if top is None or d.is_relative_to(top)]
    for directory in parents:
        candidate = directory / "pyproject.toml"
        if candidate.is_file() and toml.load(candidate).get("project", {}).get("name") == "lionagi":
            return candidate
    if any((directory / ".git").exists() for directory in parents):
        raise AssertionError(f"no lionagi pyproject.toml above {start} in a git checkout")
    return None
