# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""Detect email addresses near outbound HTTP header values in tool input.

The check scans raw command or file text, including scripts. It does not match
ordinary email use without a nearby header token. It may refuse text that merely
quotes a header and cannot detect every possible way of constructing a header.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

__all__ = (
    "ALLOWED_DOMAIN_RE",
    "EMAIL_RE",
    "HEADER_TOKEN_RE",
    "SHORT_HEADER_FLAG_RE",
    "find_leak",
    "find_leak_with_allowed",
)

# fmt: off
ALLOWED_DOMAIN_RE = re.compile(
    r"@(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)|[a-z0-9-]+\.(?:invalid|test))$", re.I
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")

HEADER_TOKEN_RE = re.compile(
    r"(?:"
    r"user[-_ ]?agent"              # User-Agent: / "User-Agent" / --user-agent / user_agent=
    r"|--header[ =]"
    r"|\bheaders?\s*[=:]"           # headers={...}, headers: {...}, header=
    r"|\bX-[A-Za-z0-9-]+\s*:"       # X-Contact: ...
    r"|setRequestHeader\s*\("
    r"|add_header\s*\("
    r"|\bheaders?\s*\[[^\]\n]{1,80}\]\s*="  # headers["From"] = ..., req.headers['User-Agent'] =
    r")",
    re.I,
)

SHORT_HEADER_FLAG_RE = re.compile(
    r"(?<![A-Za-z0-9-])-[a-zA-Z]*[AHU](?:[ =]|(?=[\"']))"  # curl -A '...', -H 'Name: value', -sH ..., wget -U ...; a quote is left for _window
    r"|(?<![A-Za-z0-9-])-[AU](?=[A-Za-z_/(])"    # glued value: -Akfin/0.1 (…), wget -Ukfin/0.1 (…)
    r"|(?<![A-Za-z0-9-])-H(?=[A-Za-z][A-Za-z0-9-]*:)"  # glued header: -HFrom: …
)

def _header_tokens(text: str):
    """Every header-shaped token in text, in document order (case-insensitive names, case-sensitive short flags)."""
    toks = list(HEADER_TOKEN_RE.finditer(text)) + list(SHORT_HEADER_FLAG_RE.finditer(text))
    toks.sort(key=lambda m: m.start())
    return toks

WINDOW = 240

# Direct calls use only built-in domains; the wrapper sets and resets extra addresses per call.
_extra_allowed: ContextVar[frozenset[str]] = ContextVar("email_header_allowed", default=frozenset())


def _allowed(address: str) -> bool:
    return address.lower() in _extra_allowed.get() or bool(ALLOWED_DOMAIN_RE.search(address))


SHORT_FLAG_RE = re.compile(r"^-[a-zA-Z]*[AHU][ =]?$")

# [ \t] not \s: a \s run at a line start spans newlines, so whitespace-only lines are
# rescanned from every line start (4000 blank lines: 350 ms, x4 per doubling; now 0.3 ms).
ASSIGN_RE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=([\"']?)(.*)$")

def _window(text: str, tok: re.Match) -> str:
    """The text a header token's value occupies. A short curl flag (`-A`, `-H`) takes exactly
    its next shell word (the quoted string if one opens there); the named tokens take a fixed
    span, since their value shapes vary (a dict literal, a `Name: value` line, a call)."""
    start = tok.end()
    if not SHORT_FLAG_RE.match(tok.group(0)):
        return text[start: start + WINDOW]
    rest = text[start:].lstrip(" ")
    if rest[:1] in ("'", '"'):
        q = rest[0]
        end = rest.find(q, 1)
        return rest[1:] if end < 0 else rest[1:end]
    return rest.split(maxsplit=1)[0] if rest.strip() else ""

def _variables_carrying_addresses(text: str) -> dict[str, str]:
    """Find non-allowed addresses in shell assignment values."""
    out = {}
    for m in ASSIGN_RE.finditer(text):
        for em in EMAIL_RE.finditer(m.group(3)):
            address = em.group(0).rstrip(".")
            if not _allowed(address):
                out[m.group(1)] = address
                break
    return out

def find_leak(text: str) -> tuple[str, str] | None:
    """(address, header token) for the first email inside a header value, else None."""
    if not text or "@" not in text:
        return None
    carriers = _variables_carrying_addresses(text)
    for tok in _header_tokens(text):
        window = _window(text, tok)
        for em in EMAIL_RE.finditer(window):
            address = em.group(0).rstrip(".")
            if _allowed(address):
                continue
            return address, tok.group(0).strip()
        for name, address in carriers.items():
            if re.search(r"\$\{?" + re.escape(name) + r"\b", window):
                return address, tok.group(0).strip()
    return None

# fmt: on


def find_leak_with_allowed(text: str, allowed: frozenset[str]) -> tuple[str, str] | None:
    """Run the predicate with this call's additional allowed addresses."""
    token = _extra_allowed.set(allowed)
    try:
        return find_leak(text)
    finally:
        _extra_allowed.reset(token)
