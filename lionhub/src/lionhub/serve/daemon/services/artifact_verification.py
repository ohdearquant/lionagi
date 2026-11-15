# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Derive the artifact-verification state shown by Studio.

Live progress and terminal verdict absence remain distinct states.
"""

from __future__ import annotations

import json
from typing import Any

from lionagi.state.artifact_verifier import (
    ArtifactPathError,
    stale_artifact_markers,
    verify_artifact_contract,
)
from lionagi.state.db import SESSION_TERMINAL_STATUSES

__all__ = ("provisional_artifact_verification", "resolve_artifact_verification")


def provisional_artifact_verification(
    contract: Any, artifacts_path: str | None
) -> dict[str, Any] | None:
    """Read live artifact progress.

    The result is explicitly provisional, never a recorded verdict.
    """
    if not artifacts_path:
        return None
    if isinstance(contract, str):
        try:
            contract = json.loads(contract)
        except ValueError:
            return None
    if not isinstance(contract, dict):
        return None

    try:
        result = verify_artifact_contract(contract, artifacts_root=artifacts_path)
    except ArtifactPathError:
        return None
    except OSError:
        return None
    return {**result, "provisional": True}


def resolve_artifact_verification(
    stored: Any,
    *,
    status: str,
    contract: Any,
    artifacts_path: str | None,
) -> Any:
    """Resolve the artifact-verification display state.

    Stored verdicts win; terminal verdict absence is represented explicitly.
    A stored verdict is a snapshot taken at run completion (`checked_at`) —
    it is labeled, not re-verified, against current disk state so a caller
    can tell a completion-time reading from current truth.
    """
    if stored is not None:
        if isinstance(stored, dict) and stored.get("status") != "not_recorded":
            markers = stale_artifact_markers(stored, artifacts_root=artifacts_path)
            if markers is not None:
                return {**stored, **markers}
            return {**stored, "staleness_check": "unknown"}
        return stored
    if not contract:
        return None
    if status == "running":
        return provisional_artifact_verification(contract, artifacts_path)
    if status in SESSION_TERMINAL_STATUSES:
        return {"status": "not_recorded"}
    return None
