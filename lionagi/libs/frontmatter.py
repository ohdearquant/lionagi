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
