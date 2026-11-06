# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Boundaries shared by the Studio host guard and schedule clients."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("machine_first", [False, True], ids=["human-first", "machine-first"])
def test_human_and_machine_url_calls_share_one_warning(monkeypatch, machine_first):
    from lionagi.cli import machine_schedule
    from lionagi.studio import cli
    from lionhub.orchestration.schedule import parsing as shared

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "https://studio.example.com/api/")
    monkeypatch.setattr(shared, "_warned_api_suffix", False)
    warnings = []
    monkeypatch.setattr(shared, "warn", warnings.append)
    first, second = (
        (machine_schedule._studio_url, cli._base_url)
        if machine_first
        else (cli._base_url, machine_schedule._studio_url)
    )
    assert first() == second() == "https://studio.example.com"
    assert len(warnings) == 1
    assert "LIONAGI_STUDIO_URL ends with /api" in warnings[0]


def test_app_import_loads_no_new_modules_from_host_normalizer():
    """Compare app-import module sets using the original and current host source."""
    baseline = Path(__file__).parent / "data" / "studio_host_before_extraction.txt"
    script = """
import importlib.util
import importlib.machinery
import json
import sys
import lionagi.studio

if sys.argv[1] == 'baseline':
    loader = importlib.machinery.SourceFileLoader('lionagi.studio._host', sys.argv[2])
    spec = importlib.util.spec_from_loader('lionagi.studio._host', loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
import lionagi.studio.app
print(json.dumps(sorted(sys.modules)))
"""
    results = []
    for mode in ("baseline", "current"):
        completed = subprocess.run(
            [sys.executable, "-c", script, mode, str(baseline)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, f"{mode} app import failed: {completed.stderr}"
        results.append(set(json.loads(completed.stdout)))
    baseline_modules, current_modules = results
    assert current_modules - baseline_modules == set()


def test_studio_reexports_the_schedule_normalizer_by_identity():
    from lionagi.studio import _host
    from lionhub.orchestration.schedule import parsing as _schedule_parsing

    assert _host.normalize_host is _schedule_parsing.normalize_host
    assert _host.normalize_host("[::ffff:192.168.1.5]") == "192.168.1.5"


def test_studio_reexports_the_shared_url_functions_by_identity():
    from lionagi.studio import cli
    from lionhub.orchestration.schedule import parsing as _schedule_parsing

    assert cli._base_url is _schedule_parsing._base_url
    assert cli._url_authority is _schedule_parsing._url_authority


def test_machine_url_does_not_import_studio(monkeypatch):
    import builtins

    from lionagi.cli import machine_schedule

    real_import = builtins.__import__

    def no_studio(name, *args, **kwargs):
        if name == "lionagi.studio" or name.startswith("lionagi.studio."):
            raise AssertionError(f"unexpected Studio import: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "https://studio.example.com")
    monkeypatch.setattr(builtins, "__import__", no_studio)
    assert machine_schedule._studio_url() == "https://studio.example.com"
