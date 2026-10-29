# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read legacy checkpoints without accepting an unsupported version as complete."""

from __future__ import annotations

import json
from typing import Any

__all__ = ("UnsupportedCheckpointVersionError", "load_legacy_checkpoint_json")


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
            if type(version) is not int or version not in (1, 2):
                raise UnsupportedCheckpointVersionError("Checkpoint version is not supported.")
    return checkpoint
