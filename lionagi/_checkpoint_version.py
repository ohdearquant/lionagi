# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read legacy checkpoints without accepting an unsupported version as complete."""

from __future__ import annotations

import json
from typing import Any

__all__ = (
    "CHECKPOINT_VERSION",
    "GRAPH_CHECKPOINT_VERSION",
    "UnsupportedCheckpointVersionError",
    "load_legacy_checkpoint_json",
)

CHECKPOINT_VERSION = 2
# A flow that runs a written graph writes this version, and only such a flow: its config
# carries a ``graph`` record, which a resume checks against the run's copy of the graph.
# Version 3 is the journaled format (ADR-0069 Amendment 2), which no reader here accepts yet.
GRAPH_CHECKPOINT_VERSION = 4
_SUPPORTED_CHECKPOINT_VERSIONS = frozenset({1, 2, 4})


def _check_writer_versions(writers: tuple[int, ...], supported: frozenset[int]) -> None:
    if any(version not in supported for version in writers):
        raise RuntimeError("A checkpoint writer version is not supported by the reader.")


_check_writer_versions(
    (CHECKPOINT_VERSION, GRAPH_CHECKPOINT_VERSION), _SUPPORTED_CHECKPOINT_VERSIONS
)


class UnsupportedCheckpointVersionError(ValueError):
    """The checkpoint version cannot be read safely by this reader."""


def load_legacy_checkpoint_json(text: str) -> Any:
    """Parse a checkpoint, refusing unsupported or ambiguous top-level versions.

    A missing version retains the old parser's behavior. Nested duplicate keys
    retain JSON's normal last-value behavior for legacy payloads.
    """
    ambiguous = False

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal ambiguous
        # The JSON decoder visits child objects first, so this flag is last
        # assigned by the top-level object when the parsed value is a mapping.
        ambiguous = sum(key == "version" for key, _ in pairs) > 1
        return dict(pairs)

    checkpoint = json.loads(text, object_pairs_hook=object_from_pairs)
    if isinstance(checkpoint, dict):
        if ambiguous:
            raise UnsupportedCheckpointVersionError("Checkpoint version is ambiguous.")
        if "version" in checkpoint:
            version = checkpoint["version"]
            if type(version) is not int or version not in _SUPPORTED_CHECKPOINT_VERSIONS:
                raise UnsupportedCheckpointVersionError("Checkpoint version is not supported.")
        _refuse_graph_version_mismatch(checkpoint)
    return checkpoint


def _refuse_graph_version_mismatch(checkpoint: dict[str, Any]) -> None:
    """Refuse a checkpoint whose version and ``graph`` record disagree.

    Version 4 is the version of a written-graph run, and its config must carry the
    ``graph`` record a resume checks; any other version, a missing one included, must not
    carry one. Without this the version would only be a label, and an edited version 2
    checkpoint with a ``graph`` record would resume with no digest check.
    """
    config = checkpoint.get("config")
    has_graph = isinstance(config, dict) and "graph" in config
    is_graph_version = checkpoint.get("version") == GRAPH_CHECKPOINT_VERSION
    if is_graph_version and not has_graph:
        raise UnsupportedCheckpointVersionError(
            "Checkpoint version is not supported: version 4 is a written-graph run's, "
            "and its config has no graph record."
        )
    if has_graph and not is_graph_version:
        raise UnsupportedCheckpointVersionError(
            "Checkpoint version is not supported: its config carries a graph record, "
            "which only a version 4 checkpoint may."
        )
