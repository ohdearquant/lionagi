# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Manifest schema v1: closed-schema validation and snapshot loading for a
declaratively fanned-out round.

A manifest names its legs by file path; this module is the refuse-early
layer that turns a path into a validated, immutable `Manifest` before
anything spawns. Every brief file named by a leg is read exactly once, here,
so the returned object owns its own snapshot bytes and a content hash — the
caller's later edits to the manifest or any brief cannot change what was
already loaded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import Any

import yaml

from lionagi._errors import LionError

__all__ = (
    "MANIFEST_VERSION",
    "MIN_LEGS",
    "MAX_LEGS",
    "MAX_TIMEOUT_SECONDS",
    "LABEL_PATTERN",
    "ManifestError",
    "Leg",
    "Manifest",
    "load_manifest",
)

MANIFEST_VERSION = 1
MIN_LEGS = 1
MAX_LEGS = 64
MAX_TIMEOUT_SECONDS = 86400

LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_TOP_LEVEL_KEYS = frozenset({"manifest_version", "defaults", "legs"})
_DEFAULTS_KEYS = frozenset({"model", "agent", "timeout"})
_LEG_KEYS = frozenset({"brief", "cwd", "label", "model", "agent", "timeout"})


class ManifestError(LionError):
    """A manifest file failed schema, path, or uniqueness validation.

    The message names the offending top-level key, `defaults` key, or leg
    (by label once its label is known to be valid, otherwise by index).
    """


@dataclass(frozen=True, slots=True)
class Leg:
    """One validated, snapshotted leg of a manifest."""

    label: str
    brief: Path
    cwd: Path
    model: str | None
    agent: str | None
    timeout: int | None
    brief_bytes: bytes
    brief_hash: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """A validated manifest: every leg snapshotted, every default resolved."""

    manifest_version: int
    legs: tuple[Leg, ...]
    default_model: str | None
    default_agent: str | None
    default_timeout: int | None


def load_manifest(path: str | Path) -> Manifest:
    """Read, parse, and validate a manifest file, snapshotting every brief.

    `path` is read once here (YAML by default, JSON when the suffix is
    `.json`); every leg's brief file is likewise read exactly once. Nothing
    in the returned `Manifest` is re-read from disk afterward.
    """
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        raise ManifestError(f"manifest path must be absolute, got {path!r}")
    resolved = manifest_path.resolve()
    if not resolved.is_file():
        raise ManifestError(f"manifest file does not exist: {resolved}")

    text = resolved.read_text()
    is_json = resolved.suffix.lower() == ".json"
    try:
        raw = json.loads(text) if is_json else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        kind = "JSON" if is_json else "YAML"
        raise ManifestError(f"manifest {resolved} is not valid {kind}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(
            f"manifest {resolved} must be a mapping at the top level, got {type(raw).__name__}"
        )
    _refuse_unknown_keys(raw, _TOP_LEVEL_KEYS, "manifest")

    version = raw.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ManifestError(f"manifest_version must be exactly {MANIFEST_VERSION}, got {version!r}")

    defaults_raw = raw.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise ManifestError(f"defaults must be a mapping, got {type(defaults_raw).__name__}")
    _refuse_unknown_keys(defaults_raw, _DEFAULTS_KEYS, "defaults")
    default_model, default_agent = _resolve_model_agent(defaults_raw, "defaults")
    default_timeout = _validate_timeout(defaults_raw.get("timeout"), "defaults")

    legs_raw = raw.get("legs")
    if not isinstance(legs_raw, list):
        raise ManifestError(f"legs must be a list, got {type(legs_raw).__name__}")
    if not (MIN_LEGS <= len(legs_raw) <= MAX_LEGS):
        raise ManifestError(
            f"legs must contain {MIN_LEGS}..{MAX_LEGS} entries, got {len(legs_raw)}"
        )

    legs: list[Leg] = []
    seen_labels: dict[str, int] = {}
    for index, leg_raw in enumerate(legs_raw):
        legs.append(
            _load_leg(leg_raw, index, seen_labels, default_model, default_agent, default_timeout)
        )

    return Manifest(
        manifest_version=version,
        legs=tuple(legs),
        default_model=default_model,
        default_agent=default_agent,
        default_timeout=default_timeout,
    )


def _refuse_unknown_keys(obj: dict, allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ManifestError(f"{where} has unknown key(s): {', '.join(unknown)}")


def _resolve_model_agent(obj: dict, where: str) -> tuple[str | None, str | None]:
    model = obj.get("model")
    agent = obj.get("agent")
    if model is not None and agent is not None:
        raise ManifestError(f"{where} names both model and agent; a level may set only one")
    if model is not None and not isinstance(model, str):
        raise ManifestError(f"{where}.model must be a string, got {type(model).__name__}")
    if agent is not None and not isinstance(agent, str):
        raise ManifestError(f"{where}.agent must be a string, got {type(agent).__name__}")
    return model, agent


def _validate_timeout(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{where}.timeout must be a positive integer, got {value!r}")
    if not (1 <= value <= MAX_TIMEOUT_SECONDS):
        raise ManifestError(
            f"{where}.timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds, got {value}"
        )
    return value


def _validate_label(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{where}.label must be a string, got {type(value).__name__}")
    lowered = value.lower()
    if not LABEL_PATTERN.fullmatch(lowered):
        raise ManifestError(
            f"{where}.label {value!r} must match {LABEL_PATTERN.pattern!r} after lowercasing"
        )
    return lowered


def _validate_brief(value: Any, where: str) -> tuple[Path, bytes, str]:
    if not isinstance(value, str):
        raise ManifestError(f"{where}.brief must be a string path, got {type(value).__name__}")
    path = Path(value)
    if not path.is_absolute():
        raise ManifestError(f"{where}.brief must be an absolute path, got {value!r}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ManifestError(f"{where}.brief does not exist or is not a regular file: {resolved}")
    data = resolved.read_bytes()
    if not data.decode("utf-8", "replace").strip():
        raise ManifestError(f"{where}.brief is empty: {resolved}")
    digest = blake2b(data).hexdigest()
    return resolved, data, digest


def _validate_cwd(value: Any, where: str) -> Path:
    if not isinstance(value, str):
        raise ManifestError(f"{where}.cwd must be a string path, got {type(value).__name__}")
    path = Path(value)
    if not path.is_absolute():
        raise ManifestError(f"{where}.cwd must be an absolute path, got {value!r}")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ManifestError(f"{where}.cwd does not exist or is not a directory: {resolved}")
    return resolved


def _load_leg(
    leg_raw: Any,
    index: int,
    seen_labels: dict[str, int],
    default_model: str | None,
    default_agent: str | None,
    default_timeout: int | None,
) -> Leg:
    where = f"legs[{index}]"
    if not isinstance(leg_raw, dict):
        raise ManifestError(f"{where} must be a mapping, got {type(leg_raw).__name__}")
    _refuse_unknown_keys(leg_raw, _LEG_KEYS, where)

    for required in ("brief", "cwd", "label"):
        if required not in leg_raw:
            raise ManifestError(f"{where} is missing required key {required!r}")

    label = _validate_label(leg_raw["label"], where)
    where = f"leg {label!r}"
    if label in seen_labels:
        raise ManifestError(f"{where} collides with legs[{seen_labels[label]}] after lowercasing")
    seen_labels[label] = index

    brief_path, brief_bytes, brief_hash = _validate_brief(leg_raw["brief"], where)
    cwd_path = _validate_cwd(leg_raw["cwd"], where)

    leg_model, leg_agent = _resolve_model_agent(leg_raw, where)
    if leg_model is not None or leg_agent is not None:
        model, agent = leg_model, leg_agent
    else:
        model, agent = default_model, default_agent

    leg_timeout = _validate_timeout(leg_raw.get("timeout"), where)
    timeout = leg_timeout if leg_timeout is not None else default_timeout

    return Leg(
        label=label,
        brief=brief_path,
        cwd=cwd_path,
        model=model,
        agent=agent,
        timeout=timeout,
        brief_bytes=brief_bytes,
        brief_hash=brief_hash,
    )
