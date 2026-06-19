# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for NavTool: outline, find_definition, find_references."""

from pathlib import Path

import pytest

from lionagi.protocols.action.tool import Tool
from lionagi.tools.code.nav import (
    NavRequest,
    NavResponse,
    NavTool,
    _find_definition_sync,
    _find_references_sync,
    _outline_sync,
)

SAMPLE_PY = """\
class Foo:
    def method(self, x, y=1):
        pass

def bar(a, b, *args, **kwargs):
    return a + b

baz = 42
"""

TYPED_PY = """\
class Base:
    pass

class Child(Base):
    def run(self) -> None:
        pass

def helper(x: int) -> str:
    return str(x)
"""


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE_PY)
    return f


@pytest.fixture()
def typed_file(tmp_path: Path) -> Path:
    f = tmp_path / "typed.py"
    f.write_text(TYPED_PY)
    return f


# ---------------------------------------------------------------------------
# _outline_sync
# ---------------------------------------------------------------------------


def test_outline_finds_class(sample_file):
    resp = _outline_sync(str(sample_file))
    assert resp.success
    kinds = {item.kind for item in resp.items}
    assert "class" in kinds
    names = [item.name for item in resp.items]
    assert "Foo" in names


def test_outline_finds_top_level_function(sample_file):
    resp = _outline_sync(str(sample_file))
    names = [item.name for item in resp.items]
    assert "bar" in names


def test_outline_finds_method(sample_file):
    resp = _outline_sync(str(sample_file))
    names = [item.name for item in resp.items]
    assert "Foo.method" in names


def test_outline_method_has_signature(sample_file):
    resp = _outline_sync(str(sample_file))
    method = next(i for i in resp.items if i.name == "Foo.method")
    assert method.signature is not None
    assert "self" in method.signature
    assert "x" in method.signature


def test_outline_function_has_signature(sample_file):
    resp = _outline_sync(str(sample_file))
    fn = next(i for i in resp.items if i.name == "bar")
    assert fn.signature is not None
    assert "*args" in fn.signature
    assert "**kwargs" in fn.signature


def test_outline_line_numbers_positive(sample_file):
    resp = _outline_sync(str(sample_file))
    for item in resp.items:
        assert item.line >= 1


def test_outline_syntax_error(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def foo(\n")  # unclosed paren
    resp = _outline_sync(str(bad))
    assert not resp.success
    assert resp.error is not None
    assert "SyntaxError" in resp.error


def test_outline_missing_file(tmp_path):
    resp = _outline_sync(str(tmp_path / "nonexistent.py"))
    assert not resp.success
    assert resp.error is not None


def test_outline_two_classes(typed_file):
    resp = _outline_sync(str(typed_file))
    names = [i.name for i in resp.items if i.kind == "class"]
    assert "Base" in names
    assert "Child" in names


# ---------------------------------------------------------------------------
# _find_definition_sync
# ---------------------------------------------------------------------------


def test_find_definition_class(sample_file):
    resp = _find_definition_sync(str(sample_file), "Foo")
    assert resp.success
    assert len(resp.items) >= 1
    assert resp.items[0].kind == "class"
    assert resp.items[0].name == "Foo"
    assert resp.items[0].line >= 1


def test_find_definition_function(sample_file):
    resp = _find_definition_sync(str(sample_file), "bar")
    assert resp.success
    assert len(resp.items) >= 1
    assert resp.items[0].kind == "function"


def test_find_definition_variable(sample_file):
    resp = _find_definition_sync(str(sample_file), "baz")
    assert resp.success
    assert len(resp.items) >= 1
    assert resp.items[0].name == "baz"


def test_find_definition_missing_symbol(sample_file):
    resp = _find_definition_sync(str(sample_file), "does_not_exist")
    assert resp.success  # no error, just empty
    assert resp.items == []


def test_find_definition_syntax_error(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("class\n")
    resp = _find_definition_sync(str(bad), "Foo")
    assert not resp.success


# ---------------------------------------------------------------------------
# _find_references_sync
# ---------------------------------------------------------------------------


def test_find_references_returns_uses(typed_file):
    resp = _find_references_sync(str(typed_file), "Base")
    assert resp.success
    assert len(resp.items) >= 1
    assert all(i.name == "Base" for i in resp.items)


def test_find_references_line_numbers_positive(typed_file):
    resp = _find_references_sync(str(typed_file), "Base")
    for item in resp.items:
        assert item.line >= 1


def test_find_references_no_uses(sample_file):
    resp = _find_references_sync(str(sample_file), "completely_unknown_xyz")
    assert resp.success
    assert resp.items == []


def test_find_references_kind(typed_file):
    resp = _find_references_sync(str(typed_file), "Base")
    for item in resp.items:
        assert item.kind == "reference"


# ---------------------------------------------------------------------------
# NavTool async handle_request
# ---------------------------------------------------------------------------


async def test_handle_request_dict_input(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("def foo(): pass\n")
    tool = NavTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request({"action": "outline", "path": str(f)})
    assert isinstance(resp, NavResponse)
    assert resp.success


async def test_handle_request_outline(tmp_path):
    f = tmp_path / "s.py"
    f.write_text(SAMPLE_PY)
    tool = NavTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(NavRequest(action="outline", path=str(f)))
    assert resp.success
    names = [i.name for i in resp.items]
    assert "Foo" in names
    assert "bar" in names


async def test_handle_request_find_definition(tmp_path):
    f = tmp_path / "s.py"
    f.write_text(SAMPLE_PY)
    tool = NavTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        NavRequest(action="find_definition", path=str(f), symbol="Foo")
    )
    assert resp.success
    assert len(resp.items) >= 1


async def test_handle_request_find_references(tmp_path):
    f = tmp_path / "t.py"
    f.write_text(TYPED_PY)
    tool = NavTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        NavRequest(action="find_references", path=str(f), symbol="Base")
    )
    assert resp.success
    assert len(resp.items) >= 1


async def test_handle_request_missing_symbol_find_def(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    tool = NavTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(NavRequest(action="find_definition", path=str(f), symbol=None))
    assert not resp.success
    assert resp.error


async def test_handle_request_unknown_action(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    tool = NavTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(NavRequest(action="unknown_action", path=str(f)))
    assert not resp.success


async def test_handle_request_rejects_outside_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir(exist_ok=True)
    f = outside / "secret.py"
    f.write_text("x = 1\n")
    tool = NavTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(NavRequest(action="outline", path=str(f)))
    assert not resp.success
    assert resp.error


# ---------------------------------------------------------------------------
# NavTool.to_tool
# ---------------------------------------------------------------------------


def test_to_tool_returns_tool():
    tool = NavTool()
    assert isinstance(tool.to_tool(), Tool)


def test_to_tool_cached():
    tool = NavTool()
    assert tool.to_tool() is tool.to_tool()


def test_to_tool_func_name():
    tool = NavTool()
    assert tool.to_tool().func_callable.__name__ == "code_nav"


# ---------------------------------------------------------------------------
# CodingToolkit integration
# ---------------------------------------------------------------------------


def test_coding_toolkit_registers_code_nav(tmp_path):
    from lionagi.session.branch import Branch
    from lionagi.tools.coding import ALL_CODING_TOOLS, DEFAULT_CODING_TOOLS, CodingToolkit

    assert "code_nav" in ALL_CODING_TOOLS
    assert "code_nav" in DEFAULT_CODING_TOOLS

    b = Branch()
    tk = CodingToolkit(workspace_root=str(tmp_path))
    tools = tk.bind(b)
    names = {t.func_callable.__name__ for t in tools}
    assert "code_nav" in names
