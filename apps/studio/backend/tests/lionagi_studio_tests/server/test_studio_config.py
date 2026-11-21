# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's config module re-exports the daemon and mirror settings that lionhub defines."""

from __future__ import annotations

import pytest

import lionhub.serve.daemon.settings as settings


@pytest.mark.parametrize("name", settings.__all__)
def test_the_old_studio_config_name_is_the_same_object(name, monkeypatch):
    import lionagi_studio.config as studio_config

    # This tree's autouse fixture turns the mirror setting off on the settings module only;
    # undo it so both modules are compared as they were imported.
    monkeypatch.undo()
    assert getattr(studio_config, name) is getattr(settings, name)


def test_config_negative_preview_chars_rejected(monkeypatch):
    import importlib

    from lionagi_studio import config as config_mod
    from lionhub.serve import mirror_settings

    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", "-1")
    with pytest.raises(ValueError):
        importlib.reload(mirror_settings)
        importlib.reload(config_mod)
    monkeypatch.delenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", raising=False)
    importlib.reload(mirror_settings)
    importlib.reload(config_mod)


def test_config_omitted_preview_chars_defaults_to_500(monkeypatch):
    import importlib

    monkeypatch.delenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", raising=False)
    from lionagi_studio import config as config_mod
    from lionhub.serve import mirror_settings

    importlib.reload(mirror_settings)
    importlib.reload(config_mod)
    assert config_mod.MIRROR_PREVIEW_CHARS == 500


# Live-path mirror bounding: oversized ingestion through the three real
# writers (claude_mirror.py, codex_mirror.py, lionhub/src/lionhub/serve/mirror.py). A unit test on
# the codec alone (above) does not prove these are wired; each test here
# writes an oversized transcript to disk and reads the row back out of a real
# StateDB after it went through the module's own tailer, never constructing
# the pointer by hand.


def test_studio_preview_limit_comes_from_the_settings_module() -> None:
    """The studio config and the mirror service read one preview limit, defined in one place."""
    import lionhub.serve.mirror as service
    from lionagi_studio import config
    from lionhub.serve import mirror_settings

    assert config.MIRROR_PREVIEW_CHARS == mirror_settings.MIRROR_PREVIEW_CHARS
    assert service.MIRROR_PREVIEW_CHARS == mirror_settings.MIRROR_PREVIEW_CHARS


def test_importing_the_studio_config_does_not_load_the_mirror_service() -> None:
    """A fresh interpreter that imports the studio config loads the settings module only."""
    import subprocess
    import sys

    probe = (
        "import sys, lionagi_studio.config; "
        "print(' '.join(sorted(m for m in sys.modules if m.startswith('lionhub.serve'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
    )
    loaded = set(out.stdout.split())
    assert "lionhub.serve.mirror_settings" in loaded
    assert {"lionhub.serve.mirror", "lionhub.serve.cli.mirror"}.isdisjoint(loaded)
