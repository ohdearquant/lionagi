# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for SearchTool: grep, find, max_results, include filter."""

import asyncio

from lionagi.protocols.action.tool import Tool
from lionagi.tools.code.search import (
    SearchAction,
    SearchRequest,
    SearchResponse,
    SearchTool,
)

# ---------------------------------------------------------------------------
# SearchAction enum
# ---------------------------------------------------------------------------


def test_search_action_grep_value():
    assert SearchAction.grep == "grep"
    assert SearchAction.grep.value == "grep"


def test_search_action_find_value():
    assert SearchAction.find == "find"
    assert SearchAction.find.value == "find"


# ---------------------------------------------------------------------------
# SearchRequest model
# ---------------------------------------------------------------------------


def test_search_request_required_fields():
    req = SearchRequest(action=SearchAction.grep, pattern="foo")
    assert req.action == SearchAction.grep
    assert req.pattern == "foo"


def test_search_request_defaults():
    req = SearchRequest(action=SearchAction.grep, pattern="x")
    assert req.path == "."
    assert req.max_results == 50
    assert req.include is None


def test_search_request_custom_fields():
    req = SearchRequest(
        action=SearchAction.find,
        pattern="*.py",
        path="/tmp",
        max_results=10,
    )
    assert req.path == "/tmp"
    assert req.max_results == 10


def test_search_request_accepts_explicit_null():
    # LLM tool calls often emit explicit null for optional fields; this must
    # validate (and pass None through) rather than fail, so the handler can
    # coalesce to its defaults.
    req = SearchRequest(action=SearchAction.grep, pattern="x", path=None, max_results=None)
    assert req.path is None
    assert req.max_results is None


async def test_grep_with_explicit_null_coalesces_to_defaults(tmp_path):
    (tmp_path / "alpha.py").write_text("def hello():\n    pass\n")
    tool = SearchTool(workspace_root=str(tmp_path))
    resp = await tool.handle_request(
        SearchRequest(action=SearchAction.grep, pattern="hello", path=None, max_results=None)
    )
    assert resp.success is True
    assert resp.count > 0


# ---------------------------------------------------------------------------
# SearchResponse model
# ---------------------------------------------------------------------------


def test_search_response_defaults():
    resp = SearchResponse(success=True)
    assert resp.content is None
    assert resp.count == 0
    assert resp.error is None


def test_search_response_failure():
    resp = SearchResponse(success=False, error="oops")
    assert resp.success is False
    assert resp.error == "oops"


# ---------------------------------------------------------------------------
# Grep: basic match
# ---------------------------------------------------------------------------


async def test_grep_finds_matching_content(tmp_path):
    (tmp_path / "alpha.py").write_text("def hello():\n    pass\n")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.grep,
            pattern="hello",
            path=str(tmp_path),
        )
    )
    assert resp.success is True
    assert resp.count > 0
    assert "hello" in resp.content


async def test_grep_returns_search_response(tmp_path):
    (tmp_path / "f.py").write_text("content\n")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.grep,
            pattern="content",
            path=str(tmp_path),
        )
    )
    assert isinstance(resp, SearchResponse)


# ---------------------------------------------------------------------------
# Grep: no matches (exit code 1, not an error)
# ---------------------------------------------------------------------------


async def test_grep_no_matches_returns_success(tmp_path):
    (tmp_path / "f.py").write_text("nothing here\n")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.grep,
            pattern="XYZZY_NONEXISTENT_9999",
            path=str(tmp_path),
        )
    )
    assert resp.success is True
    assert resp.count == 0


# ---------------------------------------------------------------------------
# Grep: regex pattern
# ---------------------------------------------------------------------------


async def test_grep_regex_matches_function_defs(tmp_path):
    (tmp_path / "code.py").write_text("def foo():\n    pass\ndef bar(x):\n    return x\n")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.grep,
            pattern=r"def\s+\w+",
            path=str(tmp_path),
        )
    )
    assert resp.success is True
    assert resp.count >= 2
    assert "foo" in resp.content
    assert "bar" in resp.content


# ---------------------------------------------------------------------------
# Grep: include filter restricts file types
# ---------------------------------------------------------------------------


async def test_grep_include_filter_restricts_to_py(tmp_path):
    (tmp_path / "match.py").write_text("FINDME\n")
    (tmp_path / "ignore.txt").write_text("FINDME\n")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.grep,
            pattern="FINDME",
            path=str(tmp_path),
            include="*.py",
        )
    )
    assert resp.success is True
    assert resp.count > 0
    for line in resp.content.splitlines():
        assert ".py" in line


# ---------------------------------------------------------------------------
# Grep: max_results capping
# ---------------------------------------------------------------------------


async def test_grep_max_results_capped(tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.py").write_text("TOKEN\n")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.grep,
            pattern="TOKEN",
            path=str(tmp_path),
            max_results=3,
        )
    )
    assert resp.success is True
    assert resp.count <= 3


# ---------------------------------------------------------------------------
# Find: glob matching
# ---------------------------------------------------------------------------


async def test_find_by_glob_finds_py_files(tmp_path):
    (tmp_path / "alpha.py").write_text("")
    (tmp_path / "beta.py").write_text("")
    (tmp_path / "data.txt").write_text("")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.find,
            pattern="*.py",
            path=str(tmp_path),
        )
    )
    assert resp.success is True
    assert resp.count >= 2
    content_lines = resp.content.splitlines()
    assert any("alpha.py" in ln for ln in content_lines)
    assert any("beta.py" in ln for ln in content_lines)


async def test_find_no_matches_returns_success(tmp_path):
    (tmp_path / "only.txt").write_text("")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.find,
            pattern="*.rs",
            path=str(tmp_path),
        )
    )
    assert resp.success is True
    assert resp.count == 0


# ---------------------------------------------------------------------------
# Find: max_results capping
# ---------------------------------------------------------------------------


async def test_find_max_results_capped(tmp_path):
    for i in range(10):
        (tmp_path / f"t{i}.py").write_text("")
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(
            action=SearchAction.find,
            pattern="*.py",
            path=str(tmp_path),
            max_results=2,
        )
    )
    assert resp.success is True
    assert resp.count <= 2


# ---------------------------------------------------------------------------
# Dict input
# ---------------------------------------------------------------------------


async def test_dict_input_accepted(tmp_path):
    (tmp_path / "x.py").write_text("hello\n")
    tool = SearchTool()
    resp = await tool.handle_request(
        {
            "action": "grep",
            "pattern": "hello",
            "path": str(tmp_path),
        }
    )
    assert resp.success is True
    assert resp.count > 0


# ---------------------------------------------------------------------------
# to_tool
# ---------------------------------------------------------------------------


def test_to_tool_returns_tool_instance():
    tool = SearchTool()
    assert isinstance(tool.to_tool(), Tool)


def test_to_tool_is_cached():
    tool = SearchTool()
    assert tool.to_tool() is tool.to_tool()


def test_to_tool_func_callable_is_async():
    tool = SearchTool()
    assert asyncio.iscoroutinefunction(tool.to_tool().func_callable)


async def test_to_tool_callable_executes(tmp_path):
    (tmp_path / "hi.py").write_text("hello\n")
    tool = SearchTool()
    result = await tool.to_tool().func_callable(action="grep", pattern="hello", path=str(tmp_path))
    assert result["success"] is True
    assert result["count"] > 0


# ---------------------------------------------------------------------------
# Grep timeout returns structured error
# ---------------------------------------------------------------------------


async def test_search_tool_grep_timeout_returns_structured_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {"timed_out": True, "stdout": "", "stderr": "", "returncode": -1},
    )

    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(action=SearchAction.grep, pattern="needle", path=".")
    )

    assert resp.success is False
    assert resp.count == 0
    assert "timed out" in (resp.error or "").lower()


# ---------------------------------------------------------------------------
# Find nonzero exit with stderr returns error response
# ---------------------------------------------------------------------------


async def test_search_tool_find_stderr_nonzero_is_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {"returncode": 1, "stdout": "", "stderr": "permission denied"},
    )

    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(action=SearchAction.find, pattern="*.py", path="/restricted")
    )

    assert resp.success is False
    assert "permission denied" in (resp.error or "").lower()


# ---------------------------------------------------------------------------
# grep: FileNotFoundError and generic Exception (lines 103-108)
# ---------------------------------------------------------------------------


async def test_grep_file_not_found_returns_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    # _subprocess_sync absorbs execution errors; returncode=-1, no exit-2 path hit
    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {
            "returncode": -1,
            "stdout": "",
            "stderr": "Execution error: grep not found",
        },
    )
    tool = SearchTool()
    resp = await tool.handle_request(SearchRequest(action=SearchAction.grep, pattern="x", path="."))
    assert resp.count == 0


async def test_grep_generic_exception_returns_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    # _subprocess_sync absorbs execution errors; returncode=-1, no exit-2 path hit
    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {"returncode": -1, "stdout": "", "stderr": "Execution error: unexpected"},
    )
    tool = SearchTool()
    resp = await tool.handle_request(SearchRequest(action=SearchAction.grep, pattern="x", path="."))
    assert resp.count == 0


# ---------------------------------------------------------------------------
# grep: exit code 2 (line 112)
# ---------------------------------------------------------------------------


async def test_grep_exit_code_2_returns_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {"returncode": 2, "stdout": "", "stderr": "grep: invalid regex"},
    )
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(action=SearchAction.grep, pattern="[invalid", path=".")
    )
    assert resp.success is False
    assert "invalid regex" in (resp.error or "")


# ---------------------------------------------------------------------------
# find: TimeoutExpired, FileNotFoundError, generic Exception (lines 132-139)
# ---------------------------------------------------------------------------


async def test_find_timeout_returns_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {"timed_out": True, "stdout": "", "stderr": "", "returncode": -1},
    )
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(action=SearchAction.find, pattern="*.py", path=".")
    )
    assert resp.success is False
    assert "timed out" in (resp.error or "").lower()


async def test_find_file_not_found_returns_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    # _subprocess_sync absorbs execution errors; returncode=-1 but no stderr triggers error path
    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {
            "returncode": -1,
            "stdout": "",
            "stderr": "Execution error: find not found",
        },
    )
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(action=SearchAction.find, pattern="*.py", path=".")
    )
    assert resp.success is False
    assert "not found" in (resp.error or "")


async def test_find_generic_exception_returns_error(monkeypatch):
    import lionagi.tools.code.search as search_mod

    # _subprocess_sync absorbs execution errors; returncode=-1 with stderr triggers error path
    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {"returncode": -1, "stdout": "", "stderr": "find I/O error"},
    )
    tool = SearchTool()
    resp = await tool.handle_request(
        SearchRequest(action=SearchAction.find, pattern="*.py", path=".")
    )
    assert resp.success is False
    assert "I/O error" in (resp.error or "")


# ---------------------------------------------------------------------------
# handle_request: unknown action fallback (line 174)
# ---------------------------------------------------------------------------


async def test_handle_request_unknown_action_returns_error():
    from unittest.mock import MagicMock

    tool = SearchTool()
    fake_req = MagicMock()
    fake_req.action = "unknown_action"
    resp = await tool.handle_request(fake_req)
    assert resp.success is False
    assert "Unknown action" in (resp.error or "")


# ---------------------------------------------------------------------------
# to_tool: custom system_tool_name triggers rename (line 192)
# ---------------------------------------------------------------------------


def test_to_tool_custom_system_tool_name():
    class CustomSearchTool(SearchTool):
        system_tool_name = "my_search"

    tool = CustomSearchTool()
    t = tool.to_tool()
    assert t.func_callable.__name__ == "my_search"


# ---------------------------------------------------------------------------
# Regression: rc=-1 (subprocess launch failure) must be success=False
# (_subprocess_sync returns rc=-1 on launch error; old code only checked
# rc==2, so rc==-1 fell through to success=True)
# ---------------------------------------------------------------------------


async def test_grep_rc_minus1_is_failure_not_success(monkeypatch):
    """rc=-1 from _subprocess_sync (launch failure) must yield success=False."""
    import lionagi.tools.code.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {
            "returncode": -1,
            "stdout": "",
            "stderr": "Execution error: grep not found",
        },
    )
    tool = SearchTool()
    resp = await tool.handle_request(SearchRequest(action=SearchAction.grep, pattern="x", path="."))
    assert resp.success is False, "rc=-1 must be a failure, not a silent empty success"
    assert resp.count == 0


async def test_grep_rc_127_is_failure(monkeypatch):
    """Any rc not in {0, 1} must be success=False (e.g. rc=127 = command not found)."""
    import lionagi.tools.code.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "_subprocess_sync",
        lambda *a, **kw: {"returncode": 127, "stdout": "", "stderr": "grep: command not found"},
    )
    tool = SearchTool()
    resp = await tool.handle_request(SearchRequest(action=SearchAction.grep, pattern="x", path="."))
    assert resp.success is False
    assert resp.count == 0
