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


def physical_lines(text: str) -> list[str]:
    """The text's lines without their endings, split at LF only (a CR before the LF is dropped).

    `str.splitlines` also splits at U+2028, U+2029, NEL, FF and VT; here those are characters
    inside a line, as they are for the formatter, so nothing after one of them starts a heading,
    an item, a row or a fence.
    """
    out = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if out and not out[-1]:
        out.pop()  # a final LF ends the last line; it does not open an empty one
    return out


def mask_fenced_code(text: str) -> str:
    """Blank fenced lines while retaining their line endings for diagnostics."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    out: list[str] = []
    for line, masked in zip(lines, fenced_code_mask(lines), strict=True):
        out.append(line[len(line.rstrip("\r\n")) :] if masked else line)
    return "".join(out)
