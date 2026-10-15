"""The registry check must find the literal version tag returned by PyPI."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    ("tags", "version", "expected"),
    [
        ("latest\n0.35.1\n0.35.2", "0.35.1", 0),
        ("latest\n0x35x1", "0.35.1", 1),
        ("0.35.10\nv0.35.1\n0.35.1rc1", "0.35.1", 1),
        ("0.35.1", "latest", 1),
        ("latest\n0.35.1", "latest", 0),
        ("-candidate", "-candidate", 0),
    ],
)
def test_registry_version_lookup_is_literal(tags: str, version: str, expected: int) -> None:
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/image-matches-pypi.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["image-matches-pypi"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "registry")
    helpers = re.findall(r"^has_tag\(\) \{.*?\}$", script, flags=re.MULTILINE)
    assert len(helpers) == 1, "the registry step must expose its tag lookup helper"

    result = subprocess.run(
        ["bash", "-c", helpers[0] + '\nhas_tag "$VERSION"'],
        env={**os.environ, "tags": tags, "VERSION": version},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected, result.stderr
