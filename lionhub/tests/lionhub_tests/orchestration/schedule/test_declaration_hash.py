# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin the serialized bytes behind persisted schedule and engine fingerprints."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lionhub.orchestration.schedule.declaration import _digest_of


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ({"z": "é", "a": [1, True, None]}, b'{"a": [1, true, null], "z": "\\u00e9"}'),
        ("flow:\n  task: café", b'"flow:\\n  task: caf\\u00e9"'),
        (Path("/workspace/model.json"), b'"/workspace/model.json"'),
        ({"v": float("nan")}, b'{"v": NaN}'),
        ({"v": float("inf")}, b'{"v": Infinity}'),
    ],
)
def test_schedule_digest_preserves_serialized_bytes(value, encoded):
    assert _digest_of(value) == hashlib.sha256(encoded).hexdigest()
