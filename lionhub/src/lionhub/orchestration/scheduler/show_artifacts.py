# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _play_dirs(show_dir: Path) -> list[Path]:
    try:
        return [p for p in sorted(show_dir.iterdir()) if p.is_dir()]
    except OSError:
        return []


_SHOW_TERMINAL_STATUSES = frozenset({"completed", "aborted"})


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
