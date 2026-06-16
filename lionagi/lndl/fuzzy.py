# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL fuzzy helpers — Phase 1 ships normalize_lndl_text only; parse_lndl_fuzzy deferred."""

import re
from typing import Any

from .errors import MissingOutBlockError  # noqa: F401 — re-exported for Phase 2 callers

__all__ = ("normalize_lndl_text",)  # parse_lndl_fuzzy removed until Phase 2 (#966)

_XML_ATTR_RE = re.compile(r'\b\w+=["\'][^"\']*["\']')


def normalize_lndl_text(text: str) -> str:
    """Normalize model-invented LNDL syntax: curly-brace tags, XML attrs, Note namespace casing."""
    text = re.sub(r"\{(lvar|lact)(\s+[^}]*)\}", r"<\1\2>", text)
    text = re.sub(r"\{/(lvar|lact)\}", r"</\1>", text)

    def _clean_tag(m: re.Match) -> str:
        tag = m.group(1)
        body = m.group(2)

        attrs = dict(re.findall(r'(\w+)=["\']([^"\']*)["\']', body))
        cleaned = _XML_ATTR_RE.sub("", body).strip()

        parts = cleaned.split() if cleaned else []
        name_val = attrs.get("name", "")
        if name_val and name_val not in " ".join(parts):
            parts.append(name_val)

        tag_body = " ".join(parts)
        return f"<{tag} {tag_body}>" if tag_body else f"<{tag}>"

    text = re.sub(r"<(lvar|lact)\s+((?:[^>])*?)>", _clean_tag, text)
    text = re.sub(r"<(lvar|lact)\s+Note\.", r"<\1 note.", text)
    return text


def parse_lndl_fuzzy(
    response: str,
    operable: Any,
    /,
    **kwargs: Any,
) -> Any:
    """Fuzzy-tolerant LNDL parser — raises NotImplementedError until Phase 2 modules land."""
    raise NotImplementedError(
        "parse_lndl_fuzzy requires Phase 2 LNDL modules (lexer/parser/ast/resolver) "
        "which have not yet been ported to main. "
        "Track progress in GitHub issue #966."
    )
