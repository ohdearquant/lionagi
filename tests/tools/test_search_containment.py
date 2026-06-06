# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for SearchTool workspace path containment.

These tests verify that path traversal attacks are rejected BEFORE the
subprocess is ever constructed — the security boundary is in
_validate_search_path(), called from handle_request() before any run_sync.

Issue: Standalone SearchTool accepted untrusted filesystem paths without
containment, allowing grep/find to traverse outside the project root.

Fix: workspace_root parameter + _validate_search_path() resolves and
asserts the path stays within the allowed root, failing closed.
"""

import pytest

from lionagi.tools.code.search import (
    SearchAction,
    SearchRequest,
    SearchResponse,
    SearchTool,
    _validate_search_path,
)

# ---------------------------------------------------------------------------
# Attack: _validate_search_path rejects escapes before subprocess launch
# ---------------------------------------------------------------------------


class TestValidateSearchPathContainment:
    """Unit tests for the path-containment guard (no subprocess needed)."""

    def test_allows_path_inside_root(self, tmp_path):
        inner = tmp_path / "project"
        inner.mkdir()
        resolved, err = _validate_search_path(str(inner), str(tmp_path))
        assert err is None
        assert resolved  # returns non-empty string

    def test_allows_path_equal_to_root(self, tmp_path):
        resolved, err = _validate_search_path(str(tmp_path), str(tmp_path))
        assert err is None

    def test_rejects_dotdot_escape(self, tmp_path):
        """Classic ../escape attack must be rejected with PermissionError."""
        inner = tmp_path / "base"
        inner.mkdir()
        attack_path = str(inner) + "/../etc/passwd"
        with pytest.raises(PermissionError, match="outside"):
            _validate_search_path(attack_path, str(inner))

    def test_rejects_absolute_path_outside_root(self, tmp_path):
        """Absolute path outside root must be refused."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(PermissionError, match="outside"):
            _validate_search_path(str(outside), str(root))

    def test_rejects_nested_dotdot_escape(self, tmp_path):
        """Nested traversal: base/sub/../../escape."""
        base = tmp_path / "base"
        sub = base / "sub"
        sub.mkdir(parents=True)
        attack_path = str(sub) + "/../../escape"
        with pytest.raises(PermissionError, match="outside"):
            _validate_search_path(attack_path, str(base))

    def test_no_workspace_root_allows_any_path(self, tmp_path):
        """Without workspace_root, no containment check is applied."""
        outside = tmp_path / "anywhere"
        outside.mkdir()
        resolved, err = _validate_search_path(str(outside), None)
        assert err is None


# ---------------------------------------------------------------------------
# Attack: handle_request refuses escaping paths before subprocess
# ---------------------------------------------------------------------------


class TestSearchToolHandleRequestContainment:
    """Integration tests: handle_request must reject escapes without launching subprocess."""

    @pytest.mark.anyio
    async def test_grep_escape_returns_error_response(self, tmp_path):
        """Path traversal on grep action returns SearchResponse(success=False)."""
        root = tmp_path / "workspace"
        root.mkdir()
        outside = tmp_path / "sensitive"
        outside.mkdir()
        (outside / "secret.txt").write_text("TOP SECRET")

        tool = SearchTool(workspace_root=str(root))
        resp = await tool.handle_request(
            SearchRequest(
                action=SearchAction.grep,
                pattern="SECRET",
                path=str(outside),
            )
        )
        # Must be refused — no results from outside workspace
        assert resp.success is False
        assert "outside" in (resp.error or "").lower()
        assert resp.count == 0

    @pytest.mark.anyio
    async def test_find_escape_returns_error_response(self, tmp_path):
        """Path traversal on find action returns SearchResponse(success=False)."""
        root = tmp_path / "workspace"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        tool = SearchTool(workspace_root=str(root))
        resp = await tool.handle_request(
            SearchRequest(
                action=SearchAction.find,
                pattern="*.py",
                path=str(outside),
            )
        )
        assert resp.success is False
        assert "outside" in (resp.error or "").lower()

    @pytest.mark.anyio
    async def test_dotdot_grep_refuses_before_subprocess(self, tmp_path, monkeypatch):
        """Verify PermissionError is raised BEFORE subprocess.run is called."""
        import lionagi.tools.code.search as search_mod

        subprocess_called = []

        def fake_run(*a, **kw):
            subprocess_called.append(1)
            raise AssertionError("subprocess.run must not be called on escaped path")

        monkeypatch.setattr(search_mod.subprocess, "run", fake_run)

        base = tmp_path / "base"
        base.mkdir()
        tool = SearchTool(workspace_root=str(base))
        resp = await tool.handle_request(
            SearchRequest(
                action=SearchAction.grep,
                pattern="x",
                path=str(base) + "/../escape",
            )
        )
        # subprocess should never have been called
        assert not subprocess_called, "subprocess.run was called despite path traversal"
        assert resp.success is False

    @pytest.mark.anyio
    async def test_search_inside_workspace_works(self, tmp_path):
        """Normal search inside workspace_root succeeds."""
        root = tmp_path / "ws"
        root.mkdir()
        (root / "code.py").write_text("def hello(): pass\n")

        tool = SearchTool(workspace_root=str(root))
        resp = await tool.handle_request(
            SearchRequest(
                action=SearchAction.grep,
                pattern="hello",
                path=str(root),
            )
        )
        assert resp.success is True
        assert resp.count > 0

    def test_search_tool_init_stores_workspace_root(self, tmp_path):
        tool = SearchTool(workspace_root=str(tmp_path))
        assert tool._workspace_root == str(tmp_path)

    def test_search_tool_default_no_workspace_root(self):
        tool = SearchTool()
        assert tool._workspace_root is None
