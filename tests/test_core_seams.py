# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Core imports stop at the package seams established before ADR-0136 moves."""

from pathlib import Path

import pytest

from tests._core_imports import ROOT, core_files, package_imports


def test_the_core_does_not_import_tools():
    files = core_files()
    assert ROOT / "lionagi" / "__init__.py" in files
    assert ROOT / "lionagi" / "_auto.py" in files
    assert ROOT / "lionagi" / "state" / "db.py" in files
    hits = [hit for path in files for hit in package_imports(path, "tools")]
    assert hits == [], hits


def test_the_import_check_sees_each_form_and_rejects_near_misses():
    probe = ROOT / "lionagi" / "session" / "_probe.py"
    source = (
        "import lionagi.tools.base as base\n"
        "from lionagi.tools.base import LionTool\n"
        "from lionagi import tools\n"
        "from ..tools.base import LionTool\n"
        "from .. import tools\n"
        "if TYPE_CHECKING:\n"
        "    from lionagi.tools import base\n"
        "def local():\n"
        "    from lionagi.tools.base import LionTool\n"
        "async def async_local():\n"
        "    import lionagi.tools\n"
        "importlib.import_module('lionagi.tools.base')\n"
        "importlib.import_module(name='lionagi.tools.base')\n"
        "importlib.import_module('.tools.base', package='lionagi')\n"
        "importlib.import_module('..tools', __package__)\n"
        "from importlib import import_module as load\n"
        "load('lionagi.tools.base')\n"
        "load(name='lionagi.tools')\n"
        "from lionagi.ln import import_module as ln_load\n"
        "ln_load('lionagi.tools.base')\n"
        "__import__('lionagi.tools.base')\n"
        "WARN = 'lionagi.tools'\n"
        "from lionagi.toolshed import x\n"
        "from . import tools\n"
        "importlib.import_module('.tools', package='other')\n"
        "importlib.import_module('.tools', package=__package__)\n"
        "__import__('.tools', globals())\n"
        "__import__('lionagi.tools', globals(), locals(), (), 1)\n"
        "importlib.import_module(name=module_name)\n"
        "importlib.import_module('lionagi.tools.' + module_name)\n"
        "load(f'lionagi.tools.{module_name}')\n"
        "import lionagi.tool\n"
        "importlib.import_module('...tools', __package__)\n"
        "importlib.import_module(name='..tools.base', package=__package__)\n"
        "from ..ln import import_module as local_load\n"
        "local_load('lionagi.tools.base')\n"
        "from .ln import import_module as other_load\n"
        "other_load('lionagi.tools.base')\n"
    )
    hits = package_imports(probe, "tools", source)
    assert sorted(int(hit.split(":")[1]) for hit in hits) == [
        1,
        2,
        3,
        4,
        5,
        7,
        9,
        11,
        12,
        13,
        14,
        15,
        17,
        18,
        20,
        21,
        34,
        36,
    ], hits


@pytest.mark.parametrize("relative", ["lionagi/__init__.py", "lionagi/session/__init__.py"])
def test_relative_imports_use_the_containing_package(relative: str):
    path = ROOT / Path(relative)
    source = (
        "from .tools import base" if path.parent.name == "lionagi" else "from ..tools import base"
    )
    assert package_imports(path, "tools", source) == [f"{relative}:1: lionagi.tools"]


def test_the_import_check_detects_the_original_branch_dependency():
    path = ROOT / "lionagi" / "session" / "branch.py"
    assert package_imports(path, "tools", "from lionagi.tools.base import LionTool") == [
        "lionagi/session/branch.py:1: lionagi.tools.base"
    ]
