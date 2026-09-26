"""Shared top-level fenced-code structure for the ADR tooling.

Fences nested in Markdown list/blockquote containers are outside this scanner.
"""

from __future__ import annotations

import re

_FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def fenced_code_mask(lines: list[str]) -> list[bool]:
    """Identify top-level fences, including delimiters, without changing their bytes.

    Recognizes 0-3 leading spaces, matching delimiter type and minimum length,
    valid backtick info strings, and an unterminated fence through EOF. This is
    not a parser for fences nested in Markdown list or blockquote containers.
    """
    mask: list[bool] = []
    closer: re.Pattern[str] | None = None
    for line in lines:
        view = line.rstrip("\r\n")
        if closer is not None:
            mask.append(True)
            if closer.fullmatch(view):
                closer = None
            continue
        match = _FENCE_OPEN.fullmatch(view)
        if match is None:
            mask.append(False)
            continue
        marker, info = match.groups()
        if marker[0] == "`" and "`" in info:
            mask.append(False)
            continue
        closer = re.compile(rf" {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*")
        mask.append(True)
    return mask


def mask_fenced_code(text: str) -> str:
    """Blank fenced lines while retaining their line endings for diagnostics."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line, masked in zip(lines, fenced_code_mask(lines), strict=True):
        out.append(line[len(line.rstrip("\r\n")) :] if masked else line)
    return "".join(out)
