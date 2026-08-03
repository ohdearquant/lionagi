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
import os
import re
import stat as stat_module
from dataclasses import dataclass, field
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
    "ENV_KEY_PATTERN",
    "RESERVED_ENV_KEYS",
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
ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# The runner's own reserved name (the leg artifact channel); a manifest that
# sets it would silently redirect harvest, so it is refused at load.
RESERVED_ENV_KEYS = frozenset({"LIONAGI_LEG_ARTIFACTS"})

_TOP_LEVEL_KEYS = frozenset({"manifest_version", "defaults", "legs"})
_DEFAULTS_KEYS = frozenset({"model", "agent", "timeout"})
_LEG_KEYS = frozenset({"brief", "cwd", "label", "model", "agent", "timeout", "env"})


class ManifestError(LionError):
    """A manifest file failed schema, path, or uniqueness validation.

    The message names the offending top-level key, `defaults` key, or leg
    (by label once its label is known to be valid, otherwise by index).
    """


class _StrictYamlLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML merge keys and duplicate mapping keys.

    `yaml.safe_load` expands a merge key (`<<`) into its target mapping and
    collapses duplicate keys last-write-wins BEFORE any schema check can see
    the raw document, so either one would smuggle values past the
    closed-schema validation. Refusing them at construction keeps what the
    validator sees identical to what the file says.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "merge keys ('<<') are not allowed in a manifest",
                    key_node.start_mark,
                )
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"unhashable mapping key {key!r}",
                    key_node.start_mark,
                ) from None
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"duplicate mapping key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclass(slots=True)
class _BriefCache:
    """Per-load snapshot cache with two keys, closing two alias routes.

    `by_literal` is keyed by the submitted pathname string: a repeated
    literal never resolves or reads a second time, so a path whose target
    is swapped between two legs cannot hand them different content —
    resolution itself is time-dependent and must happen at most once per
    spelling. `by_identity` is keyed by (st_dev, st_ino) captured by fstat
    on the descriptor actually read: distinct spellings of one physical
    file (hard links, symlink and target) coalesce onto the first accepted
    snapshot even if the file changes between their reads.
    """

    by_literal: dict[str, tuple[Path, bytes, str]] = field(default_factory=dict)
    by_identity: dict[tuple[int, int], tuple[bytes, str]] = field(default_factory=dict)


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
    # Sorted (key, value) pairs, not a dict: the manifest is a snapshot and a
    # mutable mapping on a frozen dataclass would let a caller edit the record.
    env: tuple[tuple[str, str], ...]

    @property
    def env_keys(self) -> tuple[str, ...]:
        """Declared env key names, the form the durable leg record lists."""
        return tuple(k for k, _ in self.env)


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

    def _refuse_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
        obj: dict = {}
        for key, val in pairs:
            if key in obj:
                raise ManifestError(f"manifest {resolved} has duplicate key {key!r}")
            obj[key] = val
        return obj

    try:
        if is_json:
            raw = json.loads(text, object_pairs_hook=_refuse_duplicate_json_keys)
        else:
            # _StrictYamlLoader subclasses SafeLoader; S506 only recognizes
            # the literal safe_load spelling.
            raw = yaml.load(text, Loader=_StrictYamlLoader)  # noqa: S506
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        kind = "JSON" if is_json else "YAML"
        raise ManifestError(f"manifest {resolved} is not valid {kind}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(
            f"manifest {resolved} must be a mapping at the top level, got {type(raw).__name__}"
        )
    _refuse_unknown_keys(raw, _TOP_LEVEL_KEYS, "manifest")

    version = raw.get("manifest_version")
    # type() rather than isinstance: bool is an int subclass, and a float 1.0
    # compares equal to 1 — both must be refused, only the exact integer passes.
    if type(version) is not int or version != MANIFEST_VERSION:
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
    brief_cache = _BriefCache()
    for index, leg_raw in enumerate(legs_raw):
        legs.append(
            _load_leg(
                leg_raw,
                index,
                seen_labels,
                default_model,
                default_agent,
                default_timeout,
                brief_cache,
            )
        )

    return Manifest(
        manifest_version=version,
        legs=tuple(legs),
        default_model=default_model,
        default_agent=default_agent,
        default_timeout=default_timeout,
    )


def _refuse_unknown_keys(obj: dict, allowed: frozenset[str], where: str) -> None:
    # YAML happily yields int or bool mapping keys ("1:", "on:"); refuse them
    # by name before the set arithmetic, which assumes strings.
    non_string = [key for key in obj if not isinstance(key, str)]
    if non_string:
        rendered = ", ".join(repr(key) for key in sorted(non_string, key=repr))
        raise ManifestError(f"{where} has non-string key(s): {rendered}")
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


def _read_brief_file(resolved: Path) -> tuple[bytes, tuple[int, int]]:
    """Read a brief through one descriptor, returning bytes and file identity.

    fstat on the open descriptor binds the (device, inode) identity to the
    bytes actually read; a stat beside a separate read could straddle a
    concurrent swap of the path's target.
    """
    with open(resolved, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat_module.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {resolved}")
        return fh.read(), (st.st_dev, st.st_ino)


def _validate_brief(value: Any, where: str, cache: _BriefCache) -> tuple[Path, bytes, str]:
    if not isinstance(value, str):
        raise ManifestError(f"{where}.brief must be a string path, got {type(value).__name__}")
    hit = cache.by_literal.get(value)
    if hit is not None:
        return hit
    path = Path(value)
    if not path.is_absolute():
        raise ManifestError(f"{where}.brief must be an absolute path, got {value!r}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ManifestError(f"{where}.brief does not exist or is not a regular file: {resolved}")
    try:
        data, identity = _read_brief_file(resolved)
    except OSError as exc:
        raise ManifestError(f"{where}.brief could not be read: {resolved} ({exc})") from exc
    known = cache.by_identity.get(identity)
    if known is not None:
        # A second spelling of one physical file: the first accepted snapshot
        # wins, and the fresh read is discarded.
        data, digest = known
    else:
        if not data.decode("utf-8", "replace").strip():
            raise ManifestError(f"{where}.brief is empty: {resolved}")
        digest = blake2b(data).hexdigest()
        cache.by_identity[identity] = (data, digest)
    result = (resolved, data, digest)
    cache.by_literal[value] = result
    return result


def _validate_env(value: Any, where: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ManifestError(f"{where}.env must be a mapping, got {type(value).__name__}")
    pairs: list[tuple[str, str]] = []
    for key, val in value.items():
        if not isinstance(key, str) or not ENV_KEY_PATTERN.fullmatch(key):
            raise ManifestError(f"{where}.env key {key!r} must match {ENV_KEY_PATTERN.pattern!r}")
        if key in RESERVED_ENV_KEYS:
            raise ManifestError(f"{where}.env must not set reserved key {key!r}")
        if isinstance(val, bool) or not isinstance(val, str):
            raise ManifestError(
                f"{where}.env[{key!r}] must be a string value, got {type(val).__name__}"
            )
        pairs.append((key, val))
    return tuple(sorted(pairs))


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
    brief_cache: _BriefCache,
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

    brief_path, brief_bytes, brief_hash = _validate_brief(leg_raw["brief"], where, brief_cache)
    cwd_path = _validate_cwd(leg_raw["cwd"], where)

    leg_model, leg_agent = _resolve_model_agent(leg_raw, where)
    if leg_model is not None or leg_agent is not None:
        model, agent = leg_model, leg_agent
    else:
        model, agent = default_model, default_agent

    leg_timeout = _validate_timeout(leg_raw.get("timeout"), where)
    timeout = leg_timeout if leg_timeout is not None else default_timeout

    env = _validate_env(leg_raw.get("env"), where)

    return Leg(
        label=label,
        brief=brief_path,
        cwd=cwd_path,
        model=model,
        agent=agent,
        timeout=timeout,
        brief_bytes=brief_bytes,
        brief_hash=brief_hash,
        env=env,
    )
