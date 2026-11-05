# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL text normalization — auto-fixes common model-invented syntax drift
(from XML/HTML/JSON training) into valid LNDL before the parser runs."""

from __future__ import annotations

import re

from .extract import extract_lndl_blocks

__all__ = ("normalize_lndl_text",)

_XML_ATTR_RE = re.compile(r'\b\w+=["\'][^"\']*["\']')

# Detects `<lact alias fn(args)</lact>` — opening tag ate the closing `>`,
# body starts immediately (has `(` before next `>`, closed further on).
_MISSING_GT_RE = re.compile(
    r"<(lvar|lact)\s+([A-Za-z_][\w.]*(?:\s+[A-Za-z_][\w.]*)?)\s+([^<>]*?\([^<>]*?)</\1>",
    re.DOTALL,
)


def _fix_missing_gt(text: str) -> str:
    """Repair ``<lact alias fn(args)</lact>`` → ``<lact alias>fn(args)</lact>``.
    Conservative: only fires when the opening had a call-paren and the closing tag exists."""

    def repl(m: re.Match) -> str:
        tag = m.group(1)
        head = m.group(2).strip()
        body = m.group(3).strip()
        return f"<{tag} {head}>{body}</{tag}>"

    return _MISSING_GT_RE.sub(repl, text)


def normalize_lndl_text(text: str) -> str:
    """Normalize model-invented syntax before lexing: curly-brace tags,
    XML attributes, missing-`>` opens, and ``Note.`` → ``note.`` casing."""
    if not text:
        return text

    # 0) If the model wrapped LNDL in ```lndl fenced blocks, prefer those.
    blocks = extract_lndl_blocks(text)
    if blocks:
        text = "\n\n".join(blocks)

    # 1) Repair missing-`>` opening tags BEFORE other rewrites so the
    #    subsequent passes see well-formed `<tag attrs>body</tag>` pairs.
    text = _fix_missing_gt(text)

    # 2) Curly-brace tags → angle-bracket tags
    text = re.sub(r"\{(lvar|lact)(\s+[^}]*)\}", r"<\1\2>", text)
    text = re.sub(r"\{/(lvar|lact)\}", r"</\1>", text)

    # 3) XML attributes inside opening tags → strip and promote `name=` to a token
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

    # 4) Note-namespace casing: `Note.` → `note.` — the assembler matches
    #    the `note.` prefix case-sensitively downstream.
    text = re.sub(r"<(lvar|lact)\s+Note\.", r"<\1 note.", text)
    return text
