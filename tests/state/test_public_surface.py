# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import lionagi.state as state_pkg
from lionagi.state import StateDB


def test_all_is_declared():
    assert hasattr(state_pkg, "__all__")


def test_all_contents():
    assert set(state_pkg.__all__) == {"StateDB"}


def test_statedb_importable():
    assert StateDB is state_pkg.StateDB
