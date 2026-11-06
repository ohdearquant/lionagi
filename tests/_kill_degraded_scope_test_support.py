# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Boot-time isolation shared by degraded-run service and command tests."""

import psutil
import pytest


@pytest.fixture(autouse=True)
def stable_boot_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host-scoped process identity checks deterministic."""
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_700_000_000.0)
