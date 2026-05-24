# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL fuzzy helpers — Phase 1 subset.

Phase 1 ships only :func:`normalize_lndl_text`.

:func:`parse_lndl_fuzzy` requires the Phase 2 lexer/parser/resolver/ast
modules and the ``SimilarityAlgo`` type that live on the ``beta`` branch.
Calling it raises ``NotImplementedError`` until Phase 2 lands.

# TODO(lndl-phase-2): promote parse_lndl_fuzzy from beta's fuzzy.py once
#   lexer.py / parser.py / ast.py / resolver.py / SimilarityAlgo are ported.
"""

import re
from typing import Any

from .errors import MissingOutBlockError  # noqa: F401 — re-exported for Phase 2 callers

__all__ = ("normalize_lndl_text", "parse_lndl_fuzzy")

_XML_ATTR_RE = re.compile(r'\b\w+=["\'][^"\']*["\']')


def normalize_lndl_text(text: str) -> str:
    """Normalize model-invented syntax before lexing.

    Handles:
    - Curly-brace tags: {lact X}fn(){/lact} → <lact X>fn()</lact>
    - XML attributes: <lact name="X" type="Y"> → <lact X>
    - Capitalized Note namespace: <lvar Note.X> → <lvar note.X>
    """
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
    """Fuzzy-tolerant LNDL parser — requires Phase 2 modules.

    This function is a Phase 2 feature. It depends on:
    - ``lionagi.lndl.lexer`` (Lexer, Token, TokenType)
    - ``lionagi.lndl.parser`` (Parser)
    - ``lionagi.lndl.ast`` (Lvar, Lact, OutBlock, Program)
    - ``lionagi.lndl.resolver`` (resolve_references_prefixed)
    - ``lionagi.ln.fuzzy.SimilarityAlgo``

    # TODO(lndl-phase-2): implement using beta branch's fuzzy.py once the
    #   above Phase 2 modules are ported to main.
    """
    raise NotImplementedError(
        "parse_lndl_fuzzy requires Phase 2 LNDL modules (lexer/parser/ast/resolver) "
        "which have not yet been ported to main. "
        "Track progress in GitHub issue #966."
    )
