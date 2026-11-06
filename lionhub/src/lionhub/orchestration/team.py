# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Team file reading shared by the team command and the layers that inspect teams.

This module imports no command module.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any


def read_team_json(path: Path) -> dict[str, Any] | None:
    """Read one team JSON file under a SHARED flock — the canonical
    safe-read every team-file reader goes through. Returns None (never
    raises) for a missing, unreadable, or corrupt file."""
    try:
        with open(path) as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_SH)
            try:
                raw = fp.read()
            finally:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        return json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return None
