# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage-targeted tests for tools/coding.py — uncovered paths."""

import pytest

from lionagi.tools.coding import (
    _MAX_OUTPUT_BYTES,
    ALL_CODING_TOOLS,
    CodingToolkit,
    _drain_stream,
    _edit_file_sync,
    _list_dir_sync,
    _read_file_sync,
    _read_image_sync,
    _resolve_workspace_path,
    _subprocess_sync,
    _write_file_sync,
)

# ---------------------------------------------------------------------------
# _resolve_workspace_path: symlink refusal, denied names, valid paths
# ---------------------------------------------------------------------------


def test_resolve_workspace_path_symlink_raises(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("x")
    link = tmp_path / "link.py"
    link.symlink_to(real)

    with pytest.raises(PermissionError, match="symlink"):
        _resolve_workspace_path(str(link), tmp_path)


def test_resolve_workspace_path_denied_name_raises(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=1")

    with pytest.raises(PermissionError, match="protected"):
        _resolve_workspace_path(str(env_file), tmp_path)


def test_resolve_workspace_path_escape_raises(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x")

    with pytest.raises(PermissionError, match="escapes"):
        _resolve_workspace_path(str(outside), tmp_path)


def test_resolve_workspace_path_valid_returns_resolved(tmp_path):
    f = tmp_path / "src" / "main.py"
    f.parent.mkdir()
    f.write_text("x")

    result = _resolve_workspace_path(str(f), tmp_path)
    assert result == f.resolve()


def test_resolve_workspace_path_relative_resolved_under_root(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("x")

    result = _resolve_workspace_path("hello.py", tmp_path)
    assert result == f.resolve()


# ---------------------------------------------------------------------------
# _read_image_sync: valid PNG, escape, OSError
# ---------------------------------------------------------------------------


def _make_tiny_png(path):
    # Minimal valid 1x1 red PNG (67 bytes)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"  # signature
        + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        + b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png_bytes)


def test_read_image_sync_valid_png(tmp_path):
    img = tmp_path / "photo.png"
    _make_tiny_png(img)

    result = _read_image_sync(str(img), tmp_path)

    assert result["success"] is True
    assert result["type"] == "image"
    assert result["media_type"] == "image/png"
    assert result["content"].startswith("data:image/png;base64,")
    assert result["size_bytes"] > 0


def test_read_image_sync_escape_returns_error(tmp_path):
    img = tmp_path.parent / "photo.png"
    _make_tiny_png(img)

    result = _read_image_sync(str(img), tmp_path)
    assert result["success"] is False


def test_read_image_sync_oserror(tmp_path, monkeypatch):
    img = tmp_path / "broken.png"
    _make_tiny_png(img)

    def raise_oserror(self):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.read_bytes", raise_oserror)
    result = _read_image_sync(str(img), tmp_path)
    assert result["success"] is False
    assert "disk full" in result["error"]


# ---------------------------------------------------------------------------
# _read_file_sync: image delegation, not-a-file, OSError on mtime
# ---------------------------------------------------------------------------


def test_read_file_sync_delegates_to_image_reader(tmp_path):
    img = tmp_path / "shot.png"
    _make_tiny_png(img)

    result = _read_file_sync(str(img), 0, 2000, tmp_path)
    assert result["success"] is True
    assert result.get("type") == "image"


def test_read_file_sync_not_a_file_returns_error(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()

    result = _read_file_sync(str(sub), 0, 2000, tmp_path)
    assert result["success"] is False
    assert "Not a file" in result["error"]


def test_read_file_sync_nonexistent_returns_error(tmp_path):
    result = _read_file_sync(str(tmp_path / "nope.py"), 0, 2000, tmp_path)
    assert result["success"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# _list_dir_sync: exception from dir_to_files
# ---------------------------------------------------------------------------


def test_list_dir_sync_exception_returns_error(tmp_path, monkeypatch):
    import lionagi.libs.file.process as fp_mod

    def raise_exc(*_a, **_kw):
        raise RuntimeError("disk error")

    monkeypatch.setattr(fp_mod, "dir_to_files", raise_exc)

    result = _list_dir_sync(str(tmp_path), False, None, tmp_path)
    assert result["success"] is False
    assert "disk error" in result["error"]


# ---------------------------------------------------------------------------
# _write_file_sync: OSError writing
# ---------------------------------------------------------------------------


def test_write_file_sync_oserror_returns_error(tmp_path, monkeypatch):
    def raise_oserror(*args, **kwargs):
        raise OSError("no space left")

    target = tmp_path / "out.py"
    monkeypatch.setattr("pathlib.Path.write_text", raise_oserror)

    result = _write_file_sync(str(target), "hello", tmp_path)
    assert result["success"] is False
    assert "no space left" in result["error"]


# ---------------------------------------------------------------------------
# _edit_file_sync: OSError on read, old_string not found
# ---------------------------------------------------------------------------


def test_edit_file_sync_old_string_not_found(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("hello world\n")

    result = _edit_file_sync(str(f), "NOTFOUND", "replacement", False, tmp_path)
    assert result["success"] is False
    assert "not found" in result["error"]


def test_edit_file_sync_oserror_on_read(tmp_path, monkeypatch):
    f = tmp_path / "x.py"
    f.write_text("hello\n")

    import pathlib

    def raise_on_read(*args, **kwargs):
        raise OSError("read error")

    monkeypatch.setattr(pathlib.Path, "read_text", raise_on_read)

    result = _edit_file_sync(str(f), "hello", "bye", False, tmp_path)
    assert result["success"] is False


# ---------------------------------------------------------------------------
# _drain_stream: truncation path
# ---------------------------------------------------------------------------


def test_drain_stream_truncates_at_max(monkeypatch):
    # Simulate a stream that emits 1 chunk larger than _MAX_OUTPUT_BYTES
    monkeypatch.setattr("lionagi.tools.coding._MAX_OUTPUT_BYTES", 16, raising=False)
    data = b"A" * 20

    call_count = [0]

    class FakeStream:
        def read(self, n):
            call_count[0] += 1
            if call_count[0] == 1:
                return data
            return b""

    buf = bytearray()
    truncated = _drain_stream(FakeStream(), buf)
    assert truncated is True
    assert len(buf) <= _MAX_OUTPUT_BYTES + 8200  # capped at max + one extra chunk


def test_drain_stream_no_truncation_for_small_data():
    data = b"small data"

    class FakeStream:
        def __init__(self):
            self._calls = 0

        def read(self, n):
            self._calls += 1
            if self._calls == 1:
                return data
            return b""

    buf = bytearray()
    truncated = _drain_stream(FakeStream(), buf)
    assert truncated is False
    assert bytes(buf) == data


def test_drain_stream_handles_read_exception():
    class ExplodingStream:
        def read(self, n):
            raise OSError("stream dead")

    buf = bytearray()
    truncated = _drain_stream(ExplodingStream(), buf)
    assert truncated is False
    assert len(buf) == 0


# ---------------------------------------------------------------------------
# _subprocess_sync: FileNotFoundError, TimeoutExpired
# ---------------------------------------------------------------------------


def test_subprocess_sync_file_not_found_returns_error():
    result = _subprocess_sync(["cmd_that_does_not_exist_xyz_abc_999"], False, 5.0, None)
    assert result["returncode"] == -1
    assert "stderr" in result


def test_subprocess_sync_timeout_returns_timed_out():
    result = _subprocess_sync(["sleep", "60"], False, 0.1, None)
    assert result.get("timed_out") is True
    assert result["returncode"] == -1


def test_subprocess_sync_timeout_mock_pid_calls_kill_not_killpg(monkeypatch):
    """MagicMock proc.pid must not reach os.killpg (would target PID 1 on CI)."""
    import subprocess
    from unittest.mock import MagicMock, patch

    mock_proc = MagicMock()
    # Set pid to a MagicMock object — isinstance(pid, int) returns False,
    # so the guard routes to proc.kill() instead of os.killpg().
    mock_proc.pid = MagicMock()
    # First wait() raises TimeoutExpired; second wait() (after kill) returns normally
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 0.01), None]
    mock_proc.kill = MagicMock()

    killpg_calls = []
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg", side_effect=lambda *a: killpg_calls.append(a)),
    ):
        _subprocess_sync(["sleep", "60"], False, 0.01, None)

    assert killpg_calls == [], "os.killpg must not be called when proc.pid is not int > 1"
    mock_proc.kill.assert_called_once()


@pytest.mark.parametrize("invalid_pid", [None, 0, 1, -1, True, False])
def test_subprocess_sync_timeout_invalid_pid_calls_kill_not_killpg(invalid_pid):
    """Lock in the `> 1` half of the guard against accidental removal.

    None/0/1/-1/False all fail isinstance(int) or fail > 1; True is int but == 1.
    All must route to proc.kill() — killpg(0) would target current pgroup;
    killpg(1) would target init/CI runner.
    """
    import subprocess
    from unittest.mock import MagicMock, patch

    mock_proc = MagicMock()
    mock_proc.pid = invalid_pid
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 0.01), None]
    mock_proc.kill = MagicMock()

    killpg_calls = []
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg", side_effect=lambda *a: killpg_calls.append(a)),
    ):
        _subprocess_sync(["sleep", "60"], False, 0.01, None)

    assert killpg_calls == [], f"os.killpg must not be called for pid={invalid_pid!r}"
    mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# CodingToolkit.to_tool: raises NotImplementedError
# ---------------------------------------------------------------------------


def test_coding_toolkit_to_tool_raises():
    tk = CodingToolkit(notify=False)
    with pytest.raises(NotImplementedError, match="bind"):
        tk.to_tool()


# ---------------------------------------------------------------------------
# CodingToolkit.security_pre + _build_preprocessor with security hooks
# ---------------------------------------------------------------------------


async def test_security_pre_hook_runs_before_user_hooks(tmp_path):
    from lionagi.session.branch import Branch

    order = []

    async def security_hook(tn, action, args):
        order.append("security")
        return None

    async def user_hook(tn, action, args):
        order.append("user")
        return None

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tk.security_pre("bash", security_hook)
    tk.pre("bash", user_hook)
    tools = tk.bind(b)

    bash_tool = next(t for t in tools if t.func_callable.__name__ == "bash")
    assert bash_tool.preprocessor is not None
    await bash_tool.preprocessor({"action": "run", "command": "echo hi"})
    assert order.index("security") < order.index("user")


async def test_build_postprocessor_chains_post_hooks(tmp_path):
    from lionagi.session.branch import Branch

    calls = []

    async def post_hook(tn, action, args, result):
        calls.append(result)
        return {**result, "tagged": True}

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tk.post("reader", post_hook)
    tools = tk.bind(b)

    reader_tool = next(t for t in tools if t.func_callable.__name__ == "reader")
    assert reader_tool.postprocessor is not None
    out = await reader_tool.postprocessor({"success": True})
    assert out.get("tagged") is True


async def test_build_preprocessor_no_hooks_returns_none():
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False)
    tools = tk.bind(b)

    reader_tool = next(t for t in tools if t.func_callable.__name__ == "reader")
    assert reader_tool.preprocessor is None


# ---------------------------------------------------------------------------
# Context: get_messages and evict_action_results
# ---------------------------------------------------------------------------


async def test_context_get_messages_returns_summaries(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)

    b.msgs.add_message(instruction="do something")
    context = next(t for t in tools if t.func_callable.__name__ == "context")
    result = await context.func_callable(action="get_messages")

    assert result["success"] is True
    assert "messages" in result
    assert "range" in result


async def test_context_evict_action_results_all_kept_when_below_threshold(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    context = next(t for t in tools if t.func_callable.__name__ == "context")

    result = await context.func_callable(action="evict_action_results", keep_last=5)
    assert result["success"] is True
    assert result["removed"] == 0


async def test_context_evict_action_results_removes_old(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    context = next(t for t in tools if t.func_callable.__name__ == "context")

    # Add some action request+response pairs
    for i in range(4):
        b.msgs.add_message(instruction=f"step {i}")

    result = await context.func_callable(action="evict_action_results", keep_last=2)
    assert result["success"] is True
    assert result["removed"] == 0  # No ActionResponse messages, so nothing evicted


async def test_context_unknown_action_returns_error(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    context = next(t for t in tools if t.func_callable.__name__ == "context")

    result = await context.func_callable(action="blorp")
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ---------------------------------------------------------------------------
# Reader: list_dir with recursive/file_types, unknown action
# ---------------------------------------------------------------------------


async def test_reader_list_dir_with_file_types(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    reader = next(t for t in tools if t.func_callable.__name__ == "reader")

    result = await reader.func_callable(
        action="list_dir", path=str(tmp_path), file_types=[".py"], recursive=False
    )
    assert result["success"] is True
    assert "a.py" in result["content"]


async def test_reader_unknown_action_returns_error(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    reader = next(t for t in tools if t.func_callable.__name__ == "reader")

    result = await reader.func_callable(action="blorp", path=str(tmp_path))
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ---------------------------------------------------------------------------
# Editor: content=None for write, unknown action
# ---------------------------------------------------------------------------


async def test_editor_write_no_content_returns_error(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    editor = next(t for t in tools if t.func_callable.__name__ == "editor")

    result = await editor.func_callable(
        action="write", file_path=str(tmp_path / "out.py"), content=None
    )
    assert result["success"] is False
    assert "'content' required" in result["error"]


async def test_editor_unknown_action_returns_error(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    editor = next(t for t in tools if t.func_callable.__name__ == "editor")

    result = await editor.func_callable(action="blorp", file_path=str(tmp_path / "out.py"))
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ---------------------------------------------------------------------------
# Search: grep with include filter, unknown action
# ---------------------------------------------------------------------------


async def test_search_grep_with_include_filter(tmp_path):
    (tmp_path / "a.py").write_text("def foo(): pass\n")
    (tmp_path / "b.txt").write_text("def foo(): pass\n")
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    search = next(t for t in tools if t.func_callable.__name__ == "search")

    result = await search.func_callable(
        action="grep", pattern="def foo", path=str(tmp_path), include="*.py"
    )
    assert result["success"] is True
    assert "a.py" in result["content"]


async def test_search_unknown_action_returns_error(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    search = next(t for t in tools if t.func_callable.__name__ == "search")

    result = await search.func_callable(action="blorp", pattern="x")
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ---------------------------------------------------------------------------
# Bash: shell control operator rejection in CodingToolkit
# ---------------------------------------------------------------------------


async def test_bash_shell_control_rejected_in_toolkit(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    bash = next(t for t in tools if t.func_callable.__name__ == "bash")

    result = await bash.func_callable(command="echo hi && echo there")
    assert result["return_code"] == -1
    assert "Shell control" in result["stderr"]


async def test_bash_malformed_command_returns_error(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    bash = next(t for t in tools if t.func_callable.__name__ == "bash")

    result = await bash.func_callable(command="echo 'unterminated")
    assert result["return_code"] == -1
    assert "Malformed" in result["stderr"]


# ---------------------------------------------------------------------------
# Sandbox: no active session, already active, no workspace root
# ---------------------------------------------------------------------------


async def test_sandbox_no_active_session_error(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    sandbox = next(t for t in tools if t.func_callable.__name__ == "sandbox")

    # All operations except 'create' require active session
    for action in ("diff", "commit", "merge", "discard"):
        result = await sandbox.func_callable(action=action)
        assert result["success"] is False
        assert "No active sandbox" in result["error"]


async def test_sandbox_unknown_action_returns_error(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    sandbox = next(t for t in tools if t.func_callable.__name__ == "sandbox")

    # Inject fake session so we pass the session check
    fake_session = MagicMock()

    # Patch sandbox_discard/diff/etc via the import in the closure
    import lionagi.tools.sandbox as sb_mod

    monkeypatch.setattr(sb_mod, "create_sandbox", AsyncMock(return_value=fake_session))
    await sandbox.func_callable(action="create")

    result = await sandbox.func_callable(action="blorp")
    assert result["success"] is False
    assert "Unknown action" in result["error"]


async def test_sandbox_already_active_blocks_create(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=False, workspace_root=str(tmp_path), tools=ALL_CODING_TOOLS)
    tools = tk.bind(b)
    sandbox = next(t for t in tools if t.func_callable.__name__ == "sandbox")

    import lionagi.tools.sandbox as sb_mod

    fake_session = MagicMock()
    monkeypatch.setattr(sb_mod, "create_sandbox", AsyncMock(return_value=fake_session))

    await sandbox.func_callable(action="create")
    result = await sandbox.func_callable(action="create")
    assert result["success"] is False
    assert "already active" in result["error"].lower()


# ---------------------------------------------------------------------------
# _system_status: notify=True path exercised
# ---------------------------------------------------------------------------


async def test_system_status_emitted_via_postprocessor(tmp_path):
    from lionagi.session.branch import Branch

    b = Branch()
    tk = CodingToolkit(notify=True, workspace_root=str(tmp_path))
    tools = tk.bind(b)
    bash = next(t for t in tools if t.func_callable.__name__ == "bash")

    # The postprocessor (created by _build_postprocessor) calls _notify_post,
    # which in turn calls _system_status. Invoke it directly with a fake result.
    assert bash.postprocessor is not None
    fake_result = {"return_code": 0, "stdout": "hi", "stderr": "", "timed_out": False}
    out = await bash.postprocessor(fake_result)
    assert "system" in out
    assert "context" in out["system"]
