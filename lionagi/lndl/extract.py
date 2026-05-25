# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Extract ```lndl code blocks from LLM responses."""

import re

_FENCE_PATTERN = re.compile(
    r"^(?P<fence>```|~~~)[ \t]*"
    r"(?P<lang>[\w+-]*)[ \t]*\n"
    r"(?P<code>.*?)(?<=\n)"
    r"^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def extract_lndl_blocks(text: str) -> list[str]:
    """Extract all ```lndl fenced code blocks from text, in order."""
    blocks: list[str] = []
    for match in _FENCE_PATTERN.finditer(text):
        lang = match.group("lang").lower()
        if lang == "lndl":
            blocks.append(match.group("code"))
    return blocks


__all__ = ("extract_lndl_blocks",)
