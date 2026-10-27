# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ReaderTool guidance improvements — line-prefix framing, windowed reads."""

import pytest

from lionagi.tools.file.reader import ReaderRequest, ReaderResponse, ReaderTool


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


@pytest.mark.parametrize("offset", [3, 8], ids=["at-end", "past-end"])
async def test_reader_explains_an_offset_at_or_past_the_end(tmp_path, offset):
    path = tmp_path / "lines.txt"
    path.write_text("alpha\nbeta\ngamma\n")
    reader = ReaderTool(workspace_root=tmp_path)

    response = await reader.handle_request(
        ReaderRequest(action="read", path=str(path), offset=offset)
    )
    payload = response.model_dump()

    assert response.success is True
    assert payload.get("total_lines") == 3
    assert payload.get("reached_eof") is True
    assert response.content == (
        f"Offset {offset} is at or beyond the end of the file (3 lines). "
        "Use an offset below 3 to read file content."
    )
    assert response.error is None


@pytest.mark.parametrize(
    "offset, expected_content, reached_eof",
    [(0, "1\talpha\n2\tbeta\n", False), (1, "2\tbeta\n3\tgamma\n", True)],
    ids=["before-end", "ends-at-last-line"],
)
async def test_reader_reports_the_extent_of_a_read_window(
    tmp_path, offset, expected_content, reached_eof
):
    path = tmp_path / "window.txt"
    path.write_text("alpha\nbeta\ngamma\n")
    reader = ReaderTool(workspace_root=tmp_path)

    response = await reader.handle_request(
        ReaderRequest(action="read", path=str(path), offset=offset, limit=2)
    )
    payload = response.model_dump()

    assert response.success is True
    assert response.content == expected_content
    assert payload.get("total_lines") == 3
    assert payload.get("reached_eof") is reached_eof


@pytest.mark.parametrize("offset", [0, 7], ids=["from-start", "beyond-empty"])
async def test_reader_distinguishes_an_empty_file_from_an_exhausted_window(tmp_path, offset):
    path = tmp_path / "empty.txt"
    path.write_text("")
    reader = ReaderTool(workspace_root=tmp_path)

    response = await reader.handle_request(
        ReaderRequest(action="read", path=str(path), offset=offset)
    )
    payload = response.model_dump()

    assert response.success is True
    assert response.content == ""
    assert payload.get("total_lines") == 0
    assert payload.get("reached_eof") is True


async def test_reader_counts_a_final_line_without_a_newline(tmp_path):
    path = tmp_path / "no-newline.txt"
    path.write_text("alpha\nbeta")
    reader = ReaderTool(workspace_root=tmp_path)

    response = await reader.handle_request(
        ReaderRequest(action="read", path=str(path), offset=1, limit=1)
    )
    payload = response.model_dump()

    assert response.success is True
    assert response.content == "2\tbeta"
    assert payload.get("total_lines") == 2
    assert payload.get("reached_eof") is True


def test_reader_response_accepts_unknown_file_extent():
    response = ReaderResponse(success=False, error="File is unavailable")
    payload = response.model_dump()

    assert response.content is None
    assert response.error == "File is unavailable"
    assert payload.get("total_lines") is None
    assert payload.get("reached_eof") is None
