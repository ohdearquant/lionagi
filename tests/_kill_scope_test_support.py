# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Process-probe isolation shared by command and kill-service tests."""

import psutil
import pytest


@pytest.fixture(autouse=True)
def _guard_runner_probes_in_kill_tests(request, monkeypatch):
    """Kill tests must explicitly supply every runner PID observation."""
    if request.path.name not in {
        "test_kill.py",
        "test_kill_recorded_workers.py",
        "test_kill_degraded_run.py",
        "test_smart_staleness.py",
    }:
        return

    def unexpected_runner(pid):
        raise AssertionError(f"unconfigured runner process probe: {pid}")

    monkeypatch.setattr("lionhub.orchestration.kill._runner_process_create_time", unexpected_runner)


@pytest.fixture(autouse=True)
def stable_boot_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host-scoped process identity checks deterministic."""
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_700_000_000.0)
