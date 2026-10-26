# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from lionagi.protocols.action.lion_tool import LionTool
from lionagi.protocols.action.tool import Tool
from lionagi.session.branch import Branch

pytestmark = pytest.mark.unit


def echo(value):
    return value


def test_lion_tool_remains_abstract():
    class IncompleteTool(LionTool):
        pass

    for abstract_tool in (LionTool, IncompleteTool):
        with pytest.raises(TypeError, match="abstract"):
            abstract_tool()


@pytest.mark.parametrize("form", ["class", "instance"])
def test_branch_adapts_nominal_tools(form):
    calls = []
    adapted = Tool(func_callable=echo)

    class ConcreteTool(LionTool):
        def __init__(self):
            calls.append("constructed")

        def to_tool(self) -> Tool:
            calls.append("adapted")
            return adapted

    supplied = ConcreteTool if form == "class" else ConcreteTool()
    branch = Branch()
    branch.register_tools(supplied)

    assert calls == ["constructed", "adapted"]
    assert branch.acts.registry["echo"] is adapted


def test_branch_does_not_adapt_unrelated_duck():
    calls = []

    class DuckTool:
        is_lion_system_tool = True
        system_tool_name = "duck"

        def to_tool(self) -> Tool:
            calls.append("adapted")
            return Tool(func_callable=echo)

    duck = DuckTool()
    assert not isinstance(duck, LionTool)
    branch = Branch()
    with pytest.raises(TypeError, match="Must provide"):
        branch.register_tools(duck)
    assert calls == []
    assert "echo" not in branch.acts.registry


def test_legacy_alias_is_core_lion_tool():
    from lionagi.tools.base import LionTool as LegacyLionTool

    assert LegacyLionTool is LionTool
    assert LionTool.__module__ == "lionagi.protocols.action.lion_tool"
    assert LionTool.is_lion_system_tool is True


def test_new_pickle_uses_core_module():
    payload = pickle.dumps(LionTool, protocol=0)

    assert b"clionagi.protocols.action.lion_tool\nLionTool\n" in payload
    assert b"clionagi.tools.base\nLionTool\n" not in payload
    assert pickle.loads(payload) is LionTool


def test_cold_branch_import_and_legacy_pickle():
    source_root = Path(__file__).resolve().parents[1]
    code = """
import json
import pickle
import sys
from pathlib import Path

root = Path(sys.argv[1])
assert not any(name == "lionagi.tools" or name.startswith("lionagi.tools.") for name in sys.modules)
from lionagi.session.branch import Branch
from lionagi.protocols.action.lion_tool import LionTool

tools_after_branch = sorted(
    name for name in sys.modules if name == "lionagi.tools" or name.startswith("lionagi.tools.")
)
assert Path(sys.modules[Branch.__module__].__file__).resolve() == root / "lionagi/session/branch.py"
assert Path(sys.modules[LionTool.__module__].__file__).resolve() == root / "lionagi/protocols/action/lion_tool.py"
assert not tools_after_branch, tools_after_branch
assert pickle.loads(b"clionagi.tools.base\\nLionTool\\n.") is LionTool
legacy = sys.modules["lionagi.tools.base"]
assert Path(legacy.__file__).resolve() == root / "lionagi/tools/base.py"
assert legacy.LionTool is LionTool
print(json.dumps({
    "branch_module": Branch.__module__,
    "tools_after_branch": tools_after_branch,
    "legacy_module_loaded": "lionagi.tools.base" in sys.modules,
    "legacy_pickle_is_core": legacy.LionTool is LionTool,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(source_root)],
        cwd=source_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"child stdout:\n{completed.stdout}\nchild stderr:\n{completed.stderr}"
    )
    assert json.loads(completed.stdout) == {
        "branch_module": "lionagi.session.branch",
        "tools_after_branch": [],
        "legacy_module_loaded": True,
        "legacy_pickle_is_core": True,
    }
