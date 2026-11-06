# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

import json

CONTROL = {"finite": 1.5, "negative": -0.25, "zero": 0.0, "absent": None}


def _assert_control_roundtrip(path):
    """The control payload survived the write byte-for-value."""
    loaded = json.loads(path.read_text())
    assert loaded["payload"] == CONTROL
