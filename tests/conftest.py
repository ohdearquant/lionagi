"""Shared fixtures for the core test tree."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lionhub" / "tests"))

pytest_plugins = ["tests._shared_conftest", "lionagi.testing.pytest_plugin"]
