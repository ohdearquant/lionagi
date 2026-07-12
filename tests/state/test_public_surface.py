# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import subprocess
import sys

import lionagi.state as state_pkg
from lionagi.state import StateDB


def test_all_is_declared():
    assert hasattr(state_pkg, "__all__")


def test_all_contents():
    assert set(state_pkg.__all__) == {"StateDB"}


def test_statedb_importable():
    assert StateDB is state_pkg.StateDB


def test_statedb_lazy_export_supports_import_attribute_and_hasattr():
    code = """
import sys
import lionagi.state as state

assert "lionagi.state.db" not in sys.modules
assert hasattr(state, "StateDB")
from lionagi.state import StateDB
assert StateDB is state.StateDB
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


def test_statedb_listed_in_fresh_module_dir():
    result = subprocess.run(
        [sys.executable, "-c", "import lionagi.state as s; assert 'StateDB' in dir(s)"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
