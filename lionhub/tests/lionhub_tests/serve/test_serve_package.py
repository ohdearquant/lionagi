"""Tests for the lionhub.serve package layout and import boundaries."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import lionhub


def test_no_lionhub_cli_package_directory():
    package_dir = Path(lionhub.__file__).resolve().parent
    assert not (package_dir / "cli").exists()
    assert importlib.util.find_spec("lionhub.cli") is None


@pytest.mark.parametrize("module", ["lionhub.serve.cli.catalog", "lionhub.serve.cli.main"])
def test_serve_cli_import_loads_no_server(module):
    script = f"""
import json
import sys
import {module}
print(json.dumps(sorted(name for name in sys.modules if name == 'lionhub.serve' or name.startswith('lionhub.serve.'))))
"""
    # The child imports the same lionhub this process imported, installed or not.
    env = os.environ.copy()
    source_root = str(Path(lionhub.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, env.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    loaded = json.loads(result.stdout)
    assert module in loaded
    assert not any(
        name == "lionhub.serve.mcp" or name.startswith("lionhub.serve.mcp.") for name in loaded
    )
    assert not any(
        name == "lionhub.serve.daemon" or name.startswith("lionhub.serve.daemon.")
        for name in loaded
    )
