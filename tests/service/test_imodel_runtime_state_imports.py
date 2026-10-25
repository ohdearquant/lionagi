# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The model wrapper uses runtime-state names owned by endpoint configuration."""

from __future__ import annotations

import json
import subprocess
import sys

from tests._core_imports import ROOT, package_imports

_IMODEL = ROOT / "lionagi" / "service" / "imodel.py"
_FRESH_IMPORT = """
import importlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "lionhub" / "src"))
sys.path.insert(0, str(root))
handlers_name = "lionagi.providers._agentic_handlers"
providers_at_start = sorted(name for name in sys.modules if name.startswith("lionagi.providers"))
imodel = importlib.import_module("lionagi.service.imodel")
loaded_before = handlers_name in sys.modules
providers_after_imodel = sorted(name for name in sys.modules if name.startswith("lionagi.providers"))
config = importlib.import_module("lionagi.service.connections.endpoint_config")
handlers = importlib.import_module(handlers_name)
modules = {"imodel": imodel, "config": config, "handlers": handlers}
print(json.dumps({
    "imodel_file": str(Path(imodel.__file__).resolve()),
    "files": {name: str(Path(module.__file__).resolve()) for name, module in modules.items()},
    "specs": {name: str(Path(module.__spec__.origin).resolve()) for name, module in modules.items()},
    "providers_at_start": providers_at_start,
    "providers_after_imodel": providers_after_imodel,
    "loaded_before": loaded_before,
    "loaded_after": handlers_name in sys.modules,
    "same_object": handlers.RUNTIME_STATE_NAMES is config.RUNTIME_STATE_NAMES,
    "imodel_same_object": imodel.RUNTIME_STATE_NAMES is config.RUNTIME_STATE_NAMES,
}))
"""


def test_imodel_imports_no_provider_package():
    control = """
import importlib
import lionagi.providers.openai.chat
from ..providers._agentic_handlers import RUNTIME_STATE_NAMES
importlib.import_module("lionagi.providers.anthropic.messages")
"""
    assert len(package_imports(_IMODEL, "providers", control)) == 3
    assert package_imports(_IMODEL, "providers") == []


def test_agentic_handlers_preserve_runtime_state_name_export():
    from lionagi.providers import _agentic_handlers
    from lionagi.providers._agentic_handlers import RUNTIME_STATE_NAMES
    from lionagi.service.connections import endpoint_config

    assert RUNTIME_STATE_NAMES is endpoint_config.RUNTIME_STATE_NAMES
    assert _agentic_handlers.RUNTIME_STATE_NAMES is RUNTIME_STATE_NAMES
    assert "RUNTIME_STATE_NAMES" in _agentic_handlers.__all__


def test_fresh_imodel_import_reports_provider_handler_loading():
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed script
        [sys.executable, "-c", _FRESH_IMPORT, str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["imodel_file"] == str(_IMODEL.resolve())
    expected = {
        "imodel": str(_IMODEL.resolve()),
        "config": str((ROOT / "lionagi/service/connections/endpoint_config.py").resolve()),
        "handlers": str((ROOT / "lionagi/providers/_agentic_handlers.py").resolve()),
    }
    assert report["files"] == expected
    assert report["specs"] == expected
    assert isinstance(report["loaded_before"], bool)
    assert report["loaded_after"] is True
    assert report["same_object"] is True
    assert report["imodel_same_object"] is True
    print(json.dumps(report, sort_keys=True))
    assert report["loaded_before"] is False
