from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

import yaml

_FM_OPENER = "---"
# A delimiter line is the opener alone, with only whitespace after it.
_FM_SPLIT = re.compile(rf"^{re.escape(_FM_OPENER)}\s*$", re.MULTILINE)


def _strip_frontmatter_prefix(text: str) -> str:
    """Ignore leading whitespace and Unicode format characters before parsing."""
    start = 0
    while start < len(text) and (
        text[start].isspace() or unicodedata.category(text[start]) == "Cf"
    ):
        start += 1
    return text[start:]


def _trim_frontmatter_document(text: str) -> str:
    """Remove a leading BOM, then trim whitespace."""
    return text.removeprefix("\ufeff").strip()


def _load_frontmatter_yaml(document: str) -> Any:
    try:
        return yaml.safe_load(document)
    except ValueError as exc:
        # A YAML scalar constructor can reject a value such as an invalid calendar date.
        raise yaml.YAMLError(f"frontmatter contains an invalid YAML scalar: {exc}") from exc


_DOUBLE_QUOTED_SCALAR = re.compile(r'"(?:[^"\\\r\n]|\\.)*"')


def _with_unescaped_scalars(text: str) -> str:
    """Return *text* followed by a copy whose double-quoted scalars are unescaped.

    The checks on text that does not read as YAML look for a key's literal spelling, so a key
    written with escapes, such as ``"\\x74ools"``, would pass them. Matching against the
    returned text finds both spellings. A scalar whose escapes do not decode is copied as
    written. Text with no backslash is returned unchanged.
    """
    if "\\" not in text:
        return text

    def unescape(match: re.Match[str]) -> str:
        scalar = match.group()
        if "\\" not in scalar:
            return scalar
        try:
            value = yaml.safe_load(scalar)
        except yaml.YAMLError:
            return scalar
        return f'"{value}"' if isinstance(value, str) else scalar

    return text + "\n" + _DOUBLE_QUOTED_SCALAR.sub(unescape, text)


def _frontmatter_parts(text: str) -> list[str] | None:
    """Split *text* at its frontmatter delimiters.

    When the first line of *text* is a bare ``---``, returns the pieces of
    ``_FM_SPLIT.split(text, maxsplit=2)``: three when a closing delimiter follows, two when
    none does. Returns ``None`` when the first line is anything else, so nothing in the file
    is read as settings. Callers remove a leading byte-order mark and whitespace first.
    """
    if _FM_SPLIT.match(text) is None:
        return None
    return _FM_SPLIT.split(text, maxsplit=2)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    return _parse_frontmatter(text, _load_frontmatter_yaml)


def _parse_frontmatter_for_save(text: str) -> tuple[dict[str, Any], str]:
    """Read frontmatter with the existing save-time scalar validation."""
    return _parse_frontmatter(text, yaml.safe_load)


def _parse_frontmatter(text: str, load_yaml: Callable[[str], Any]) -> tuple[dict[str, Any], str]:
    text = _trim_frontmatter_document(text)
    parts = _frontmatter_parts(text)
    if parts is None or len(parts) < 3:
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
    raises too, instead of being coerced into an empty metadata dict. A first line
    that starts with ``---`` but holds anything else raises as well; from such a
    file ``parse_frontmatter`` reads no settings at all.
    """
    text = _trim_frontmatter_document(text)
    parts = _frontmatter_parts(text)
    if parts is None:
        if text.startswith(_FM_OPENER):
            raise yaml.YAMLError("frontmatter opening line must be a bare ---")
        return {}, text
    if len(parts) < 3:
        raise yaml.YAMLError("frontmatter opening delimiter has no matching closing delimiter")
    fm = _load_frontmatter_yaml(parts[1])
    if not isinstance(fm, dict):
        raise yaml.YAMLError("frontmatter must be a YAML mapping, not a list or scalar")
    return fm, parts[2].strip()
