# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The former catalog paths remain lazy aliases during the compatibility period."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORWARDED_NAMES = (
    "build_cli_parser",
    "command_exists",
    "iter_cli_seeds",
    "load_cli_command",
    "seed_for",
)


def _fresh(source: str, *args: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "lionhub" / "src"), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-c", source, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", FORWARDED_NAMES)
def test_legacy_catalog_name_is_the_canonical_object(name):
    old = importlib.import_module("lionagi._auto")
    catalog = importlib.import_module("lionhub.cli.catalog")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert getattr(old, name) is getattr(catalog, name)


@pytest.mark.parametrize("name", FORWARDED_NAMES)
def test_legacy_catalog_name_warns_with_its_replacement_and_release_window(name):
    _fresh(
        """
import sys
import warnings
import lionagi._auto as old

name = sys.argv[1]
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", DeprecationWarning)
    getattr(old, name)
deprecations = [item for item in caught if issubclass(item.category, DeprecationWarning)]
assert len(deprecations) == 1, [str(item.message) for item in deprecations]
warning = deprecations[0]
message = str(warning.message)
assert f"lionagi._auto.{name}" in message
assert f"lionhub.cli.catalog.{name}" in message
assert "0.37.0" in message and "2.0.0" in message
assert warning.filename == "<string>"
""",
        name,
    )


def test_legacy_catalog_module_import_defers_lionhub_until_a_name_is_used():
    _fresh(
        """
import sys
import warnings

def loaded_hub_modules():
    return [name for name in sys.modules if name == "lionhub" or name.startswith("lionhub.")]

assert not loaded_hub_modules()
import lionagi._auto as old
assert not loaded_hub_modules(), loaded_hub_modules()
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    forwarded = old.seed_for
assert "lionhub.cli.catalog" in sys.modules
from lionhub.cli import catalog
assert forwarded is catalog.seed_for
"""
    )
