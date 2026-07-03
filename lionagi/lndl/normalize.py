# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL text normalization — auto-fix common model-invented syntax errors.

Models trained on XML/HTML/JSON sometimes drift into related-but-wrong forms.
This module catches the most common drifts and rewrites them into valid LNDL
before the parser runs, so the model isn't penalized for surface mistakes.

Ported from krons.lndl.fuzzy.
"""

from __future__ import annotations

import re

from .extract import extract_lndl_blocks

__all__ = ("normalize_lndl_text",)

_XML_ATTR_RE = re.compile(r'\b\w+=["\'][^"\']*["\']')

# `<lact alias fn(args)</lact>` — opening tag ate the closing `>`, the body
# starts immediately. Detect by an opening tag containing `(` before the next
# `>` and a `</lact>`/`</lvar>` further on.
_MISSING_GT_RE = re.compile(
    r"<(lvar|lact)\s+([A-Za-z_][\w.]*(?:\s+[A-Za-z_][\w.]*)?)\s+([^<>]*?\([^<>]*?)</\1>",
    re.DOTALL,
)


def _fix_missing_gt(text: str) -> str:
    """Repair ``<lact alias fn(args)</lact>`` → ``<lact alias>fn(args)</lact>``.

    Conservative: only touches tags where (a) the opening had a function
    call (paren) inside it and (b) the closing tag is present. Common gpt
    drift; cheap to fix; harmless if it never matches.
    """

    def repl(m: re.Match) -> str:
        tag = m.group(1)
        head = m.group(2).strip()
        body = m.group(3).strip()
        return f"<{tag} {head}>{body}</{tag}>"

    return _MISSING_GT_RE.sub(repl, text)


def normalize_lndl_text(text: str) -> str:
    """Normalize model-invented syntax before lexing.

    Handles:
    - Curly-brace tags: ``{lact X}fn(){/lact}`` or ``{lact X}fn()</lact>``
      → ``<lact X>fn()</lact>``
    - XML attributes: ``<lact name="X" type="Y">`` → ``<lact X>``
    - Tag opening missing ``>`` before body: ``<lact a fn()</lact>`` →
      ``<lact a>fn()</lact>`` (when the body contains a parenthesised call).
    - ``Note.`` namespace casing: ``<lvar Note.draft d>`` → ``<lvar note.draft d>``
      (the note namespace and OUT{} ``note.x`` refs are matched case-sensitively
      downstream, so model-invented capitalization must be normalized here).
    """
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

    # 4) Note-namespace casing: `Note.` → `note.` inside tag bodies, so both
    #    the note lvar declaration and any `note.x` OUT{} ref match the
    #    case-sensitive `note.` prefix the assembler checks for.
    text = re.sub(r"<(lvar|lact)\s+Note\.", r"<\1 note.", text)
    return text
