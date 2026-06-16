# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0029: Artifact contract validation and verification."""

from __future__ import annotations

import os
import re
import time
from pathlib import PurePosixPath
from typing import Any, Literal, TypedDict

from lionagi.libs.path_safety import GLOB_CHARS as _GLOB_CHARS

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VALIDATION_ROOT = os.path.realpath("/tmp/__contract_validate__")  # noqa: S108 — synthetic root for path-validation only, never written to

# ADR-0029 §2: v1 entry fields. `kind`, `min_size`, `mime_type` are
# reserved for v1.1 — silently accepting them now would let contract
# files drift into looking stricter than the executor actually is.
# Both the `li play check` pre-flight AND the real `li play` runtime
# path emit a warning for unknown subfields via warn_unknown_artifact_keys().
_ARTIFACT_ENTRY_ALLOWED_KEYS = frozenset({"id", "path", "required", "description", "source"})


class ArtifactPathError(ValueError):
    """Raised when an artifact contract id/path is invalid."""


class ExpectedArtifact(TypedDict, total=False):
    id: str
    path: str
    required: bool
    description: str
    source: str


class ProducedArtifact(TypedDict):
    id: str
    path: str
    size: int
    present: bool


class ArtifactContract(TypedDict):
    expected: list[ExpectedArtifact]


class VerificationResult(TypedDict):
    status: Literal["passed", "failed", "warning", "skipped"]
    checked_at: float
    missing_required: list[ExpectedArtifact]
    missing_optional: list[ExpectedArtifact]
    produced: list[ProducedArtifact]


def _safe_join(root: str, rel: str) -> str:
    """Join rel under root, rejecting absolute paths, globs, '..', and escapes."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\x00" in rel:
        raise ArtifactPathError(f"absolute path not allowed: {rel!r}")
    if any(c in _GLOB_CHARS for c in rel):
        raise ArtifactPathError(f"glob characters not allowed in v1: {rel!r}")

    parts = PurePosixPath(rel).parts
    if not parts or any(p in ("..", "") for p in parts):
        raise ArtifactPathError(f"`..` segments not allowed: {rel!r}")

    root_real = os.path.realpath(root)
    joined = os.path.realpath(os.path.join(root_real, *parts))
    try:
        common = os.path.commonpath([root_real, joined])
    except ValueError as exc:
        raise ArtifactPathError(f"path escapes artifacts_root: {rel!r}") from exc
    if common != root_real:
        raise ArtifactPathError(f"path escapes artifacts_root: {rel!r}")
    return joined


def warn_unknown_artifact_keys(
    contract: dict[str, Any] | None,
    *,
    source: str = "playbook",
    emit: Any = None,
) -> list[str]:
    """Warn about unrecognized subfields in expected[] entries (ADR-0029 v1.1-reserved fields); returns messages."""
    if contract is None:
        return []
    expected = contract.get("expected") or []
    if not isinstance(expected, list):
        return []
    if emit is None:
        emit = print
    warnings: list[str] = []
    for entry in expected:
        if not isinstance(entry, dict):
            continue
        unknown = set(entry.keys()) - _ARTIFACT_ENTRY_ALLOWED_KEYS
        if unknown:
            msg = (
                f"warning: {source} artifact entry "
                f"{entry.get('id', '<unnamed>')!r} has unknown subfield(s) "
                f"{sorted(unknown)} (ignored by v1; reserved for v1.1)."
            )
            warnings.append(msg)
            emit(msg)
    return warnings


def validate_artifact_contract(contract: dict[str, Any] | None) -> None:
    if contract is None:
        return
    if not isinstance(contract, dict):
        raise ArtifactPathError(f"artifact contract must be a dict, got {type(contract).__name__}")
    expected = contract.get("expected")
    if not isinstance(expected, list):
        raise ArtifactPathError("artifact contract must contain expected: list")

    seen_ids: set[str] = set()
    for idx, entry in enumerate(expected):
        if not isinstance(entry, dict):
            raise ArtifactPathError(f"expected[{idx}] must be a dict")
        eid = entry.get("id")
        if not isinstance(eid, str) or not _ARTIFACT_ID_RE.fullmatch(eid):
            raise ArtifactPathError(f"id must be alphanumeric/_/-: {eid!r}")
        if eid in seen_ids:
            raise ArtifactPathError(f"duplicate id in contract: {eid!r}")
        seen_ids.add(eid)

        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise ArtifactPathError(f"expected[{idx}].path must be a non-empty string")
        if "required" in entry and not isinstance(entry["required"], bool):
            raise ArtifactPathError(f"expected[{idx}].required must be a bool")
        if "description" in entry and not isinstance(entry["description"], str):
            raise ArtifactPathError(f"expected[{idx}].description must be a string")
        if "source" in entry and not isinstance(entry["source"], str):
            raise ArtifactPathError(f"expected[{idx}].source must be a string")

        _safe_join(_VALIDATION_ROOT, path)


def resolve_artifact_contract(
    *,
    playbook_artifacts: dict[str, Any] | None,
    agent_defaults: dict[str, Any] | None,
) -> ArtifactContract | None:
    if playbook_artifacts is None and agent_defaults is None:
        return None

    by_id: dict[str, ExpectedArtifact] = {}
    for source, declared in (
        ("agent_profile", agent_defaults),
        ("playbook", playbook_artifacts),
    ):
        if declared is None:
            continue
        if not isinstance(declared, dict):
            raise ArtifactPathError(f"{source} artifact contract must be a dict")
        expected = declared.get("expected")
        if not isinstance(expected, list):
            raise ArtifactPathError(f"{source} artifact contract must contain expected: list")
        for raw in expected:
            if not isinstance(raw, dict):
                raise ArtifactPathError(f"{source} expected artifact must be a dict")
            spec: ExpectedArtifact = {
                **raw,
                "required": raw.get("required", True),
                "description": raw.get("description", ""),
                "source": source,
            }
            by_id[spec["id"]] = spec

    resolved: ArtifactContract = {"expected": list(by_id.values())}
    validate_artifact_contract(resolved)
    return resolved


def verify_artifact_contract(
    contract: dict[str, Any] | None,
    *,
    artifacts_root: str | None,
) -> VerificationResult | None:
    if contract is None:
        return None
    validate_artifact_contract(contract)
    expected = contract["expected"]

    if not artifacts_root or not os.path.isdir(artifacts_root):
        mr = [e for e in expected if e.get("required", True)]
        mo = [e for e in expected if not e.get("required", True)]
        if mr:
            st: Literal["failed", "warning", "passed"] = "failed"
        elif mo:
            st = "warning"
        else:
            st = "passed"
        return {
            "status": st,
            "checked_at": time.time(),
            "missing_required": mr,
            "missing_optional": mo,
            "produced": [],
        }

    root = os.path.realpath(artifacts_root)
    missing_required: list[ExpectedArtifact] = []
    missing_optional: list[ExpectedArtifact] = []
    produced: list[ProducedArtifact] = []

    for entry in expected:
        full = _safe_join(root, entry["path"])
        present = os.path.isfile(full) and os.path.getsize(full) > 0
        if present:
            produced.append(
                {
                    "id": entry["id"],
                    "path": entry["path"],
                    "size": os.path.getsize(full),
                    "present": True,
                }
            )
        elif entry.get("required", True):
            missing_required.append(entry)
        else:
            missing_optional.append(entry)

    if missing_required:
        status: Literal["failed", "warning", "passed"] = "failed"
    elif missing_optional:
        status = "warning"
    else:
        status = "passed"
    return {
        "status": status,
        "checked_at": time.time(),
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "produced": produced,
    }


def missing_artifact_summary(missing: list[dict[str, Any]]) -> str:
    if len(missing) == 1:
        entry = missing[0]
        return f"Missing required artifact: {entry.get('id')} ({entry.get('path')})."
    return f"Missing {len(missing)} required artifacts."


def missing_artifact_evidence(missing: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "kind": "expected_artifact",
            "id": str(entry.get("id", "")),
            "label": str(entry.get("path", "")),
        }
        for entry in missing
    ]
