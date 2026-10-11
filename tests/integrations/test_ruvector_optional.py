"""The native backend must remain outside the default import graph."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def test_core_import_without_ruvector():
    code = """
import importlib.abc
import sys
class MissingRuVector(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ruvector' or fullname.startswith('ruvector.'):
            raise ModuleNotFoundError('native backend absent', name='ruvector')
sys.meta_path.insert(0, MissingRuVector())
import lionagi
import lionagi.integrations
assert 'ruvector' not in sys.modules
try:
    import lionagi.integrations.ruvector
except ImportError as exc:
    assert "lionagi[ruvector]" in str(exc)
else:
    raise AssertionError('missing optional extra must fail explicitly')
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
