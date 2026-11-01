# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Agent contracts are supplied by the core rather than imported by it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from tests._core_imports import ROOT, core_files, package_imports


def test_core_has_no_agent_imports():
    violations = [
        hit for path in core_files(root=ROOT) for hit in package_imports(path, "agent", root=ROOT)
    ]
    assert not violations, "Core imports agent contracts:\n" + "\n".join(violations)


def test_agent_import_check_sees_all_forms_and_rejects_near_misses():
    path = ROOT / "lionagi" / "session" / "_probe.py"
    source = (
        "from lionagi.agent.gate import GateResult\n"
        "from ..agent import gate\n"
        "def local():\n"
        "    from lionagi.agent import settings\n"
        "if TYPE_CHECKING:\n"
        "    from lionagi import agent\n"
        "importlib.import_module(name='lionagi.agent.gate')\n"
        "importlib.import_module('..agent', __package__)\n"
        "from importlib import import_module as load\n"
        "load('lionagi.agent')\n"
        "from lionagi.agentic import something\n"
        "from . import agent\n"
        "__import__('.agent', globals())\n"
        "load(module_name)\n"
        "importlib.import_module('.agent', package='lionagi')\n"
        "importlib.import_module('.agent', package=__package__)\n"
        "from ..ln import import_module as local_load\n"
        "local_load('lionagi.agent.gate')\n"
    )
    hits = package_imports(path, "agent", source)
    assert sorted(int(hit.split(":")[1]) for hit in hits) == [1, 2, 4, 6, 7, 8, 10, 15, 18], hits


@pytest.mark.parametrize(
    "import_legacy_settings", [False, True], ids=["core-only", "legacy-import-control"]
)
def test_secret_settings_resolution_keeps_agent_unloaded_in_a_fresh_process(
    tmp_path, import_legacy_settings
):
    home = tmp_path / "home"
    settings_dir = home / ".lionagi"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.yaml").write_text(
        'secrets:\n  lookup:\n    argv: [lookup-secret, "{name}"]\n'
        "    names: [LIONAGI_PROBE_TOKEN]\n"
    )
    work = tmp_path / "work"
    work.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    source_paths = [str(ROOT), str(ROOT / "lionhub" / "src")]
    if env.get("PYTHONPATH"):
        source_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(source_paths)
    script = textwrap.dedent(
        """
        import importlib
        import json
        from pathlib import Path
        import sys

        root = Path(sys.argv[1]).resolve()

        def agent_modules():
            return sorted(
                name for name in sys.modules
                if name == "lionagi.agent" or name.startswith("lionagi.agent.")
            )

        before = agent_modules()
        assert before == [], before
        from lionagi.service.accounts import _secret_resolution

        assert Path(_secret_resolution.__file__).resolve() == (
            root / "lionagi/service/accounts/_secret_resolution.py"
        )
        assert agent_modules() == []
        result = _secret_resolution.resolve_secret_lookup_config(settings=None)
        assert result.reason is None
        assert result.lookup.argv == ("lookup-secret", "{name}")
        assert result.lookup.names == ("LIONAGI_PROBE_TOKEN",)
        after_resolution = agent_modules()
        assert after_resolution == [], after_resolution
        from lionagi import _settings

        assert Path(_settings.__file__).resolve() == root / "lionagi/_settings.py"
        if sys.argv[2] == "legacy":
            legacy = importlib.import_module("lionagi.agent.settings")
            assert Path(legacy.__file__).resolve() == root / "lionagi/agent/settings.py"
            assert legacy.load_settings is _settings.load_settings
            assert agent_modules()
        print(json.dumps({
            "before": before,
            "after_resolution": after_resolution,
            "after_control": agent_modules(),
        }))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), "legacy" if import_legacy_settings else "core"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    probe = json.loads(completed.stdout)
    assert probe["before"] == []
    assert probe["after_resolution"] == []
    assert bool(probe["after_control"]) is import_legacy_settings
