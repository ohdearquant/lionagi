from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

import yaml

_FM_SPLIT = re.compile(r"^---\s*$", re.MULTILINE)


def _strip_frontmatter_prefix(text: str) -> str:
    """Ignore leading whitespace and Unicode format characters before parsing."""
    start = 0
    while start < len(text) and (
        text[start].isspace() or unicodedata.category(text[start]) == "Cf"
    ):
        start += 1
    return text[start:]


def _load_frontmatter_yaml(document: str) -> Any:
    try:
        return yaml.safe_load(document)
    except ValueError as exc:
        # A YAML scalar constructor can reject a value such as an invalid calendar date.
        raise yaml.YAMLError(f"frontmatter contains an invalid YAML scalar: {exc}") from exc


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    return _parse_frontmatter(text, _load_frontmatter_yaml)


def _parse_frontmatter_for_save(text: str) -> tuple[dict[str, Any], str]:
    """Read frontmatter with the existing save-time scalar validation."""
    return _parse_frontmatter(text, yaml.safe_load)


def _parse_frontmatter(text: str, load_yaml: Callable[[str], Any]) -> tuple[dict[str, Any], str]:
    text = text.strip()
    if not text.startswith("---"):
        return {}, text
    parts = _FM_SPLIT.split(text, maxsplit=2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = load_yaml(parts[1]) or {}
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
    fm = _load_frontmatter_yaml(parts[1])
    if not isinstance(fm, dict):
        raise yaml.YAMLError("frontmatter must be a YAML mapping, not a list or scalar")
    return fm, parts[2].strip()
