# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Extract ```lndl code blocks from LLM responses."""

import re

# A fence may be indented, may be longer than three characters (so a block can
# quote a shorter fence), and may end its lines with CRLF. It closes only on a
# line holding the same fence at the same indentation, so a block opened at the
# margin keeps an indented fence inside a value as content.
_FENCE_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*"
    r"(?P<lang>[\w+-]*)[ \t]*\r?\n"
    r"(?P<code>.*?)(?<=\n)"
    r"^(?P=indent)(?P=fence)[ \t]*\r?$",
    re.MULTILINE | re.DOTALL,
)


def extract_lndl_blocks(text: str) -> list[str]:
    """Extract all ```lndl fenced code blocks from text, in order, with CRLF line ends read as LF."""
    blocks: list[str] = []
    for match in _FENCE_PATTERN.finditer(text):
        lang = match.group("lang").lower()
        if lang == "lndl":
            blocks.append(match.group("code").replace("\r\n", "\n"))
    return blocks


__all__ = ("extract_lndl_blocks",)
