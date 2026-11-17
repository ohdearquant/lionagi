# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ReaderTool guidance improvements — line-prefix framing, windowed reads."""

import pytest

from lionhub.tools.file import reader as reader_mod
from lionhub.tools.file.reader import ReaderRequest, ReaderResponse, ReaderTool


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


def test_reader_preserves_universal_newlines(tmp_path):
    path = tmp_path / "mixed.txt"
    raw = b"alpha\rbeta\r\ngamma\x0bdelta\ninvalid:\xff"
    path.write_bytes(raw)

    response = reader_mod._read_sync(str(path), 1, 3, tmp_path)

    assert response.success
    assert response.content == "2\tbeta\n3\tgamma\x0bdelta\n4\tinvalid:\ufffd"
    assert response.total_lines == 4
    assert response.reached_eof is True


def _over_budget_file(tmp_path, monkeypatch):
    path = tmp_path / "big.log"
    path.write_bytes(b"".join(b"row %06d\n" % i for i in range(20_000)))
    monkeypatch.setattr(reader_mod, "_MAX_TEXT_SCAN_BYTES", 16_384)
    monkeypatch.setattr(reader_mod, "_SCAN_CHUNK_CHARS", 4_096)
    return path


def test_a_window_inside_the_scan_budget_returns_unknown_counts(tmp_path, monkeypatch):
    path = _over_budget_file(tmp_path, monkeypatch)

    response = reader_mod._read_sync(str(path), 2, 2, tmp_path)

    assert response.success
    assert response.content == "3\trow 000002\n4\trow 000003\n"
    assert response.total_lines is None
    assert response.reached_eof is None


def test_an_offset_beyond_the_scan_budget_is_an_error(tmp_path, monkeypatch):
    path = _over_budget_file(tmp_path, monkeypatch)

    response = reader_mod._read_sync(str(path), 15_000, 5, tmp_path)

    assert not response.success
    assert "16,384 bytes" in response.error
    assert response.content is response.total_lines is response.reached_eof is None


@pytest.mark.parametrize("tail", [b"", b"\nnext\n"], ids=["no-newline", "then-next-line"])
def test_a_line_longer_than_the_line_cap_is_cut(tmp_path, monkeypatch, tail):
    path = tmp_path / "one-line.txt"
    path.write_bytes(b"x" * 1_000 + tail)
    monkeypatch.setattr(reader_mod, "_MAX_LINE_CHARS", 100)
    monkeypatch.setattr(reader_mod, "_SCAN_CHUNK_CHARS", 32)

    response = reader_mod._read_sync(str(path), 0, 10, tmp_path)

    assert response.success
    cut = "1\t" + "x" * 100 + " [... line truncated at 100 characters]"
    if tail:
        assert response.content == cut + "\n2\tnext\n"
        assert response.total_lines == 2
    else:
        assert response.content == cut
        assert response.total_lines == 1
    assert response.reached_eof is True


def test_directory_scan_counts_filtered_entries_across_depth(tmp_path, monkeypatch):
    (tmp_path / "ignore.bin").touch()
    (tmp_path / "shown.py").touch()
    child = tmp_path / "child"
    child.mkdir()
    (child / "nested.py").touch()
    monkeypatch.setattr(reader_mod, "_MAX_DIRECTORY_ENTRIES", 3)

    top_only = reader_mod._list_dir_sync(str(tmp_path), False, [".py"], tmp_path)
    assert top_only.success
    assert top_only.content == str(tmp_path / "shown.py")

    # Four entries in all; the fourth is past the limit whichever order they come in.
    recursive = reader_mod._list_dir_sync(str(tmp_path), True, [".py"], tmp_path)
    assert recursive.success
    *listed, notice = recursive.content.split("\n")
    assert notice == (
        "[listing stopped after inspecting 3 entries; narrow the path or file_types to see the rest]"
    )
    assert set(listed) <= {str(tmp_path / "shown.py"), str(child / "nested.py")}

    monkeypatch.setattr(reader_mod, "_MAX_DIRECTORY_ENTRIES", 4)
    recursive = reader_mod._list_dir_sync(str(tmp_path), True, [".py"], tmp_path)
    assert recursive.success
    assert set(recursive.content.split("\n")) == {
        str(tmp_path / "shown.py"),
        str(child / "nested.py"),
    }


def test_directory_listing_stops_at_the_file_limit_with_a_notice(tmp_path, monkeypatch):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).touch()
    monkeypatch.setattr(reader_mod, "_MAX_LIST_FILES", 2)

    response = reader_mod._list_dir_sync(str(tmp_path), False, None, tmp_path)

    assert response.success
    *listed, notice = response.content.split("\n")
    assert len(listed) == 2 and notice == "[listing stopped at 2 files]"


def test_an_unreadable_subdirectory_is_skipped_in_a_recursive_listing(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads any directory")
    (tmp_path / "top.py").touch()
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.py").touch()
    locked.chmod(0)
    try:
        files, notice = reader_mod._scan_files(tmp_path, True, None)
    finally:
        locked.chmod(0o755)

    assert [f.name for f in files] == ["top.py"] and notice is None


def test_an_unreadable_base_directory_still_fails(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads any directory")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            reader_mod._scan_files(locked, True, None)
    finally:
        locked.chmod(0o755)


def _reference_window(raw: bytes, start: int, max_lines: int):
    import io

    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace") as stream:
        lines = stream.readlines()
    return lines[start : start + max_lines], len(lines)


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64])
def test_a_chunked_scan_matches_reading_every_line(tmp_path, monkeypatch, chunk):
    import random

    monkeypatch.setattr(reader_mod, "_SCAN_CHUNK_CHARS", chunk)
    rng = random.Random(chunk)
    alphabet = [b"a", b"b", b"\n", b"\r", b"\r\n", b"\x0b", "é".encode(), b"\xff"]
    path = tmp_path / "mixed.txt"
    for _ in range(60):
        raw = b"".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        path.write_bytes(raw)
        start, count = rng.randrange(0, 12), rng.randrange(1, 6)
        lines, total = _reference_window(raw, start, count)

        response = reader_mod._read_sync(str(path), start, count, tmp_path)

        assert response.success, raw
        assert response.total_lines == total, raw
        if total and start >= total:
            assert response.content.startswith(f"Offset {start} is at or beyond"), raw
        else:
            expected = "".join(f"{start + i + 1}\t{line}" for i, line in enumerate(lines))
            assert response.content == expected, raw


def test_one_read_returns_a_bounded_amount_of_text(tmp_path, monkeypatch):
    path = tmp_path / "wide.txt"
    path.write_text("".join(f"{i:09d}\n" for i in range(100)), encoding="utf-8")
    monkeypatch.setattr(reader_mod, "_MAX_TEXT_OUTPUT_CHARS", 25)

    response = reader_mod._read_sync(str(path), 0, 1_000_000, tmp_path)

    assert response.success
    assert response.content == "1\t000000000\n2\t000000001\n3\t000000002\n"
    assert response.total_lines == 100
    assert response.reached_eof is False


def test_a_line_cut_by_the_scan_budget_is_marked(tmp_path, monkeypatch):
    path = tmp_path / "one-line.txt"
    path.write_bytes(b"z" * 100_000)
    monkeypatch.setattr(reader_mod, "_MAX_TEXT_SCAN_BYTES", 16_384)
    monkeypatch.setattr(reader_mod, "_SCAN_CHUNK_CHARS", 4_096)

    response = reader_mod._read_sync(str(path), 0, 5, tmp_path)

    assert response.success
    assert response.total_lines is None and response.reached_eof is None
    assert response.content.startswith("1\tzzz")
    assert response.content.endswith(" [... read stopped at the 16,384-byte scan limit]")
