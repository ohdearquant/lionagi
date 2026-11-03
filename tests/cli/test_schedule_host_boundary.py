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

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "https://studio.example.com/api/")
    monkeypatch.setattr(cli, "_warned_api_suffix", False)
    warnings = []
    monkeypatch.setattr(cli, "warn", warnings.append)
    first, second = (
        (machine_schedule._studio_url, cli._base_url)
        if machine_first
        else (cli._base_url, machine_schedule._studio_url)
    )
    assert first() == second() == "https://studio.example.com"
    assert len(warnings) == 1
    assert "LIONAGI_STUDIO_URL ends with /api" in warnings[0]


def test_app_import_loads_no_new_modules_from_host_normalizer():
    """Compare fresh imports against the original host source, in the same environment."""
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
            check=True,
            timeout=30,
        )
        results.append(set(json.loads(completed.stdout)))
    baseline_modules, current_modules = results
    assert current_modules - baseline_modules == set()


def test_studio_reexports_the_schedule_normalizer_by_identity():
    from lionagi.cli import _schedule_parsing
    from lionagi.studio import _host

    assert _host.normalize_host is _schedule_parsing.normalize_host
    assert _host.normalize_host("[::ffff:192.168.1.5]") == "192.168.1.5"


def test_schedule_parser_has_no_studio_imports():
    from lionagi.cli import _schedule_parsing
    from tests.cli._import_guard import resolved_imports

    imported = resolved_imports(Path(_schedule_parsing.__file__), "lionagi.cli")
    assert not {
        name for name in imported if name == "lionagi.studio" or name.startswith("lionagi.studio.")
    }
