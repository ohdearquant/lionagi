from __future__ import annotations

import re
from typing import Any

import yaml

_FM_SPLIT = re.compile(r"^---\s*$", re.MULTILINE)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = text.strip()
    if not text.startswith("---"):
        return {}, text
    parts = _FM_SPLIT.split(text, maxsplit=2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm if isinstance(fm, dict) else {}, parts[2].strip()


def parse_frontmatter_strict(text: str) -> tuple[dict[str, Any], str]:
    """Like ``parse_frontmatter``, but raises on a broken frontmatter block instead of
    discarding it silently. For save-time validation, where a swallowed parse error
    would let bad content through and only surface the next time it's read.

    Unlike ``parse_frontmatter``, an opening ``---`` with no matching closing
    ``---`` is treated as malformed (raises) rather than as "no frontmatter
    present" -- and a frontmatter block that parses to something other than a
    YAML mapping (including an explicit ``null`` document, or an empty body)
    raises too, instead of being coerced into an empty metadata dict.
    """
    text = text.strip()
    if not text.startswith("---"):
        return {}, text
    parts = _FM_SPLIT.split(text, maxsplit=2)
    if len(parts) < 3:
        raise yaml.YAMLError("frontmatter opening delimiter has no matching closing delimiter")
    fm = yaml.safe_load(parts[1])
    if not isinstance(fm, dict):
        raise yaml.YAMLError("frontmatter must be a YAML mapping, not a list or scalar")
    return fm, parts[2].strip()
