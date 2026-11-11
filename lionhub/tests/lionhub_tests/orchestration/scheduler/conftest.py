# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_claude_mirror(monkeypatch):
    """Keep Studio's in-process mirror disabled for tests that need its settings."""
    import lionagi.studio.config as config_mod

    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", False)
