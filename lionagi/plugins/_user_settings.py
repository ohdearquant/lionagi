# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read/write helper for the plugin-related blocks of ``~/.lionagi/settings.yaml``.

Trust records (D5) and the enable/disable flag (D7) are both user-level, never
project-level: a repository must not be able to self-trust a plugin it
carries by committing a settings line — the human on the machine approves.
This mirrors ``lionagi.agent.settings.load_settings`` (which merges global
and project settings for *reading* hooks/config) but is scoped to writing the
one file a plugin operator actually controls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

__all__ = (
    "user_settings_path",
    "read_user_settings",
    "write_user_settings",
)


def user_settings_path() -> Path:
    return Path.home() / ".lionagi" / "settings.yaml"


def read_user_settings() -> dict[str, Any]:
    path = user_settings_path()
    if not path.is_file():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def write_user_settings(data: dict[str, Any]) -> None:
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
