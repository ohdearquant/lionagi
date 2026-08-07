# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0064: Artifact contract validation and verification."""

from __future__ import annotations

import os
import re
import time
from pathlib import PurePosixPath
from typing import Any, Literal, TypedDict

from lionagi.libs.path_safety import GLOB_CHARS as _GLOB_CHARS

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VALIDATION_ROOT = os.path.realpath("/tmp/__contract_validate__")  # noqa: S108 — synthetic root for path-validation only, never written to

# v1 entry fields. `kind`/`min_size`/`mime_type` reserved for v1.1 — unknown
# subfields warn via warn_unknown_artifact_keys() (ADR-0064 D3) rather than silently pass.
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


class StaleMarkers(TypedDict):
    staleness_check: Literal["checked"]
    changed_since_verification: list[str]
    absent_since_verification: list[str]


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
    """Warn about unrecognized subfields in expected[] entries (v1.1-reserved fields); returns messages."""
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


def _resolve_produced(root: str, rel: str) -> str | None:
    """Return the path an expected artifact was actually produced at, or None.
    A bare filename is matched at the root first, then in any immediate
    subdirectory (sorted, for a deterministic match) — see
    docs/internals/runtime.md for why a bare filename must resolve this way
    in a multi-agent run."""
    try:
        direct = _safe_join(root, rel)
    except ArtifactPathError:
        return None
    if os.path.isfile(direct):
        return direct

    if len(PurePosixPath(rel).parts) != 1:
        return None

    try:
        subdirs = sorted(entry.name for entry in os.scandir(root) if entry.is_dir())
    except OSError:
        return None

    for sub in subdirs:
        try:
            candidate = _safe_join(root, f"{sub}/{rel}")
        except ArtifactPathError:
            continue  # not a legal path segment, so not somewhere we'd have written
        if os.path.isfile(candidate):
            return candidate
    return None


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
        full = _resolve_produced(root, entry["path"])
        present = full is not None and os.path.getsize(full) > 0
        if present:
            produced.append(
                {
                    "id": entry["id"],
                    "path": os.path.relpath(
                        full, root
                    ),  # where it was found, not just what was asked for
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


def stale_artifact_markers(
    verification: dict[str, Any], *, artifacts_root: str | None
) -> StaleMarkers | None:
    """Cheaply flag whether a recorded verdict's produced artifacts may no
    longer match what was on disk at `checked_at`.

    This never re-verifies pass/fail — it only checks mtime+size and presence
    for the artifacts the recorded verdict already found, so a caller can
    label the verdict's currency instead of presenting a completion-time
    snapshot as current state. Comparing size alongside mtime (both already
    available from a single `stat()` call, so this stays a cheap read-time
    check rather than a re-verify) narrows, though does not close, the
    false-negative window a bare mtime check would have against a rewrite
    that preserves both mtime and size.

    Returns None only when the check cannot be performed at all — no
    `artifacts_root`, or a verdict missing the `checked_at`/`produced` fields
    the check needs (e.g. a payload recorded before this check existed). The
    caller uses that to report an explicit unknown state rather than
    silently treating an unchecked verdict as clean.
    """
    if not artifacts_root:
        return None
    checked_at = verification.get("checked_at")
    produced = verification.get("produced")
    if not isinstance(checked_at, (int, float)) or not isinstance(produced, list):
        return None

    root = os.path.realpath(artifacts_root)
    changed: list[str] = []
    absent: list[str] = []
    for entry in produced:
        if not isinstance(entry, dict):
            continue
        artifact_id = entry.get("id")
        rel_path = entry.get("path")
        if not artifact_id or not rel_path:
            continue
        try:
            full = _safe_join(root, rel_path)
        except ArtifactPathError:
            continue
        try:
            stat_result = os.stat(full)
        except OSError:
            absent.append(artifact_id)
            continue
        declared_size = entry.get("size")
        size_changed = isinstance(declared_size, int) and stat_result.st_size != declared_size
        if stat_result.st_mtime > checked_at or size_changed:
            changed.append(artifact_id)

    return {
        "staleness_check": "checked",
        "changed_since_verification": changed,
        "absent_since_verification": absent,
    }
