"""Canonical, row-bound identity for unverifiable process observations."""

from __future__ import annotations

import math
from typing import Any


def quarantine_identity(row: dict[str, Any]) -> tuple[int, float | None, str | None] | None:
    """Return the metadata-backed process identity, or None when no safe PID is bound."""
    metadata = row.get("node_metadata")
    if not isinstance(metadata, dict):
        return None
    raw_pid = metadata.get("pid")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, (int, str)):
        return None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    if pid <= 0:
        return None
    raw_created = metadata.get("pid_create_time")
    created = None
    if raw_created is not None and not isinstance(raw_created, bool):
        try:
            candidate = float(raw_created)
        except (TypeError, ValueError, OverflowError):
            pass
        else:
            if math.isfinite(candidate) and candidate > 0:
                created = candidate
    # A tuple that changes and reverts between sweeps is unobservable; the
    # next observation can only compare the identity actually recorded here.
    generation = row.get("cli_worker_generation")
    if generation is not None and not isinstance(generation, str):
        return None
    return pid, created, generation
