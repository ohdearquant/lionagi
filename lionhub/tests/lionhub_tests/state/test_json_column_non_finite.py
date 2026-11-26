# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The state store's JSON columns refuse non-finite floats rather than store null."""

import pytest


def test_db_json_column_rejects_non_finite():
    """The stored row is what cannot be repaired later, so this path checks."""
    from lionhub.state.db import _to_json_column

    assert _to_json_column({"v": 1.5}) == '{"v":1.5}'
    with pytest.raises(ValueError, match="non-finite float at \\$\\.v"):
        _to_json_column({"v": float("inf")})
