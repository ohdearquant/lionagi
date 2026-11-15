"""The daemon's settings live in lionhub.serve.daemon.settings and stay importable from Studio's config."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import lionhub.serve.daemon.settings as settings

_ROOT = Path(__file__).resolve().parents[5]


@pytest.mark.parametrize("name", settings.__all__)
def test_the_old_studio_config_name_is_the_same_object(name):
    import lionagi.studio.config as studio_config

    assert getattr(studio_config, name) is getattr(settings, name)


@pytest.mark.parametrize(
    "path",
    [
        "lionagi/studio/app.py",
        "lionhub/src/lionhub/serve/daemon/launch_access.py",
        "lionhub/src/lionhub/serve/daemon/services/pairing.py",
    ],
)
def test_daemon_readers_take_the_settings_from_lionhub(path):
    """A patch of the lionhub module reaches these readers only if they import from it."""
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("config"):
            moved = {alias.name for alias in node.names} & set(settings.__all__)
            assert not moved, f"{path} reads {sorted(moved)} from {node.module}"
