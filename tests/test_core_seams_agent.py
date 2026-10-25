# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Agent contracts are supplied by the core rather than imported by it."""

from __future__ import annotations

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
