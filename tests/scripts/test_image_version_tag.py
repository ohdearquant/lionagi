"""The registry check must find the literal version tag returned by PyPI."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


def _tag_helper(script: str) -> str:
    helpers = re.findall(r"^[ \t]*has_tag\(\)[^\n]*$", script, flags=re.MULTILINE)
    assert len(helpers) == 1, "the registry step must expose its tag lookup helper"
    return helpers[0]


@pytest.mark.parametrize(
    ("tags", "version", "expected"),
    [
        ("latest\n0.35.1\n0.35.2", "0.35.1", 0),
        ("latest\n0x35x1", "0.35.1", 1),
        ("0.35.10\nv0.35.1\n0.35.1rc1", "0.35.1", 1),
        ("0.35.1", "latest", 1),
        ("latest\n0.35.1", "latest", 0),
        ("-candidate", "-candidate", 0),
        pytest.param(
            "0.35.1\n" + "\n".join(f"0.34.{index}" for index in range(30_000)),
            "0.35.1",
            0,
            id="match-before-a-large-tag-list",
        ),
    ],
)
def test_registry_version_lookup_is_literal(
    tags: str, version: str, expected: int, tmp_path: Path
) -> None:
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/image-matches-pypi.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["image-matches-pypi"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "registry")
    # A file avoids platform-specific environment-variable size limits.
    tag_file = tmp_path / "tags.txt"
    tag_file.write_text(tags)

    result = subprocess.run(
        [
            "bash",
            "-eo",
            "pipefail",
            "-c",
            'tags=$(<"$TAGS_FILE")\n' + _tag_helper(script) + '\nhas_tag "$VERSION"',
        ],
        env={**os.environ, "TAGS_FILE": str(tag_file), "VERSION": version},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected, result.stderr


def test_helper_extraction_accepts_indentation_and_a_trailing_comment() -> None:
    helper = '  has_tag() { grep -qxF -- "$1" <<<"${tags}"; } # literal tag'
    assert _tag_helper("before\n" + helper + "\nafter\n") == helper


def test_helper_extraction_rejects_multiple_definitions() -> None:
    with pytest.raises(AssertionError, match="must expose its tag lookup helper"):
        _tag_helper("has_tag() { :; }\n  has_tag() { :; } # duplicate\n")
