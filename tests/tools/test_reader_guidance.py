# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ReaderTool guidance improvements — line-prefix framing, windowed reads."""

from lionagi.tools.file.reader import ReaderRequest, ReaderTool

# ---------------------------------------------------------------------------
# Output format: each line is `<number>\t<code>` — strip prefix before editing
# ---------------------------------------------------------------------------


async def test_reader_output_has_numbered_lines(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("x = 1\ny = 2\n")
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="read", path=str(f)))
    assert resp.success
    lines = resp.content.splitlines()
    # Each line starts with a digit followed by a tab
    for line in lines:
        assert "\t" in line, f"Expected tab separator in line: {line!r}"
        prefix, _ = line.split("\t", 1)
        assert prefix.strip().isdigit(), f"Expected numeric prefix, got: {prefix!r}"


async def test_reader_line_prefix_is_not_in_original_content(tmp_path):
    """The number+tab prefix is added by the reader — it is NOT part of the file."""
    f = tmp_path / "check.py"
    content = "def foo():\n    return 42\n"
    f.write_text(content)
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="read", path=str(f)))
    assert resp.success
    # Strip the prefixes and reconstruct — should match original
    stripped = ""
    for line in resp.content.splitlines(keepends=True):
        _, code = line.split("\t", 1)
        stripped += code
    assert stripped == content


async def test_reader_windowed_read_offset_limit(tmp_path):
    """offset+limit slices return the correct window."""
    f = tmp_path / "big.py"
    lines = [f"line_{i} = {i}\n" for i in range(100)]
    f.write_text("".join(lines))
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="read", path=str(f), offset=10, limit=5))
    assert resp.success
    result_lines = resp.content.splitlines()
    assert len(result_lines) == 5
    # First line should have number prefix 11 (1-based)
    first_prefix = result_lines[0].split("\t", 1)[0]
    assert first_prefix.strip() == "11"


async def test_reader_offset_zero_reads_from_start(tmp_path):
    f = tmp_path / "z.py"
    f.write_text("a = 1\nb = 2\n")
    rt = ReaderTool(workspace_root=str(tmp_path))
    resp = await rt.handle_request(ReaderRequest(action="read", path=str(f), offset=0, limit=1))
    assert resp.success
    lines = resp.content.splitlines()
    assert len(lines) == 1
    assert "a = 1" in lines[0]


async def test_reader_path_required_error_guides_actions(tmp_path):
    """path=None path on standalone ReaderTool gives an actionable message."""
    rt = ReaderTool(workspace_root=str(tmp_path))
    # Build request with a valid path to avoid pydantic validation error,
    # then override after construction for the internal guard check.
    req = ReaderRequest(action="read", path="x")
    req.path = ""  # bypass pydantic to hit the runtime guard
    resp = await rt.handle_request(req)
    assert not resp.success
    assert "path" in resp.error.lower()
