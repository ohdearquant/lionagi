# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Live stream text is safe to include in terminal output."""

import json

import pytest

from lionhub.serve.cli import monitor


@pytest.mark.parametrize(
    ("control", "visible"),
    [
        ("\x1b[2J", "[2J"),
        ("\x1b]52;c;dGVzdA==\x1b\\", "]52;c;dGVzdA==\\"),
        ("\x1b]0;title\x07", "]0;title"),
        ("\u202e", ""),
        ("\x9b2J", "2J"),
        ("\u2028\u2029", ""),
    ],
    ids=["esc-csi", "osc52", "osc-title", "bidi", "c1-csi", "separators"],
)
@pytest.mark.parametrize("shape", ["normalized", "legacy", "malformed"])
def test_stream_tail_filters_terminal_controls(tmp_path, shape, control, visible):
    stream = tmp_path / "stream"
    stream.mkdir()
    text = "café\t中文" + control + "end"
    if shape == "normalized":
        row = json.dumps({"_lionagi_stream_chunk": True, "type": "text", "content": text})
    elif shape == "legacy":
        row = json.dumps({"delta": {"text": text}})
    else:
        row = text
    (stream / "branch.buffer.jsonl").write_text(row + "\n", encoding="utf-8")

    assert monitor._stream_tail(tmp_path, "branch") == ["café\t中文" + visible + "end"]
