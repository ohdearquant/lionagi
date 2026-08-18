# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared bounds and redaction helpers for the Studio Operator read tools.

``public_project`` mirrors the helper already inline in ``application_mcp.py``
(extracted here so a second read-service module can use it without importing
back into that module — see ``run_progress.py``/``run_findings.py``). The
caps below match the bounds the existing read tools already apply
(``list_recent_runs`` returns at most 20, Operator context values are
truncated at 2 KB in ``engine.py``); no new tool widens what the Operator can
see about secrets, tokens, or absolute host paths.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from lionagi.libs.credential_fields import (
    EXACT_SECRET_FIELD_NAMES,
    SECRET_KEY_MARKERS,
    fold_field_name,
    is_secret_field_name,
)

__all__ = (
    "MAX_CANDIDATES",
    "PER_KIND_ITEM_CAP",
    "PER_ITEM_TEXT_CAP",
    "MESSAGE_BYTE_CAP",
    "ARTIFACT_BYTE_CAP",
    "public_project",
    "scrub_text",
    "fold_field_name",
    "is_secret_field_name",
    "known_secret_values",
    "redact_scalar",
    "redact_arguments",
    "cap_by_bytes",
    "cap_payload_by_bytes",
)

# Reference resolution never returns more candidates than this — matches the
# bounded-projection pattern list_recent_runs/list_schedules already use.
MAX_CANDIDATES = 10
# Per branch, per finding kind (messages/tool_calls/errors) in run_findings.
PER_KIND_ITEM_CAP = 50
# A single message/tool-call text field is trimmed to this many characters
# before the item is returned, so one oversized field cannot dominate a
# bounded response.
PER_ITEM_TEXT_CAP = 8_000
# Aggregate bound for one findings section (messages, tool_calls, or errors)
# across every branch in a run, applied after the per-item/per-kind caps.
MESSAGE_BYTE_CAP = 2 * 1024 * 1024
# Bound for one artifact projection field (contract or verification), applied
# after redaction — a single field is never allowed to exceed the same
# aggregate bound the other findings sections use.
ARTIFACT_BYTE_CAP = 2 * 1024 * 1024

# Backward-compatible private aliases used by the cross-layer agreement test.
# The source of truth lives in libs.credential_fields.
_EXACT_SECRET_FIELD_NAMES = EXACT_SECRET_FIELD_NAMES
_SECRET_KEY_MARKERS = SECRET_KEY_MARKERS


def public_project(value: Any) -> str | None:
    """Reduce a project/path value to a leaf name so no filesystem layout is
    disclosed. Identical logic to ``application_mcp.public_project`` —
    duplicated rather than imported to avoid a load-time circular import
    between the new read-service modules and the tool-registry module."""
    if not isinstance(value, str) or not value:
        return None
    if Path(value).is_absolute():
        return Path(value).name or "external-project"
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute():
        return windows_path.name or "external-project"
    return value[:160]


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# A path segment ordinarily has no spaces (``_SEG_WORD``); an intermediate
# directory segment (not the leaf) may additionally contain up to seven
# single-space-separated words, so a real path like
# ``/Users/lion/My Project/private notes/secret.txt`` is matched and redacted
# in full instead of only its first (no-space) component. The leaf itself
# stays a plain word so the match cannot run on into surrounding prose past
# the file name.
_SEG_WORD = r"[\w.\-]+"
_SEG_MULTI = _SEG_WORD + r"(?: " + _SEG_WORD + r"){0,6}"
_ABS_POSIX_RE = re.compile(
    r"(?<![\w/])(/" + _SEG_MULTI + r"(?:/" + _SEG_MULTI + r")*/" + _SEG_WORD + r")"
)
_ABS_WIN_RE = re.compile(r"(?<![\w])([A-Za-z]:\\(?:" + _SEG_MULTI + r"\\)*" + _SEG_WORD + r")")
_SECRET_TOKEN_RE = re.compile(
    r"(?<![\w])((?:sk|ghp|gho|ghu|ghs|xox[baprs]|AKIA)[A-Za-z0-9_\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)
# A bare "Bearer <token>" with no field name in front of it (e.g. embedded in
# a free-text tool-call argument or command string).
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+\S+")
# Names whose value is an RFC 7235 "<scheme> <credentials>" pair rather than a
# bare secret. The scheme names a mechanism and is not itself a credential, so
# it survives; what follows it does not.
_AUTH_PAIR_FIELD_NAMES = frozenset(
    {"authorization", "proxy_authorization", "www_authenticate", "proxy_authenticate"}
)
_AUTH_SCHEME = (
    r"(?:Bearer|Basic|Digest|Token|ApiKey|Negotiate|Mutual|HOBA|OAuth|vapid"
    r"|SCRAM-SHA-1|SCRAM-SHA-256)"
)
# The gap between a recognized scheme and its credential. A header folded
# across lines, or one pasted into free text, puts a line break here; matching
# horizontal whitespace alone consumed the scheme and left the credential
# behind on the next line, which is worse than not matching at all. Bounded to
# a single break so a blank line ends the value rather than reaching further
# down the text.
_AUTH_PAIR_GAP = r"(?:[ \t]+|[ \t]*\r?\n[ \t]*)"
# The same gap for a scheme this list does not recognize, where the break has
# to look like an actual fold. A recognized scheme is its own evidence that
# what follows it is a credential, so that branch accepts any break. Here there
# is no such evidence and the second token is taken on trust, so requiring the
# continuation to begin with a space or tab -- which is what a folded header
# is, per RFC 7230 obs-fold -- separates one from an ordinary next line of
# prose, whose first word would otherwise be taken. Leaving it on one line
# instead left the credential of every unrecognized scheme sitting in the text
# one line below the marker that announced it.
_AUTH_PAIR_FOLD = r"(?:[ \t]+|[ \t]*\r?\n[ \t]+)"
# An auth header in free text. An unrecognized scheme falls to the second
# branch, which takes both tokens rather than leaving the credential behind
# the word in front of it.
_AUTH_PAIR_RE = re.compile(
    r"(?i)(?<![\w.\-])((?:proxy[_\-]?)?authorization|(?:www|proxy)[_\-]?authenticate)"
    r"(\s*[:=]\s*)"
    r"(?:(" + _AUTH_SCHEME + r")(" + _AUTH_PAIR_GAP + r")\S+|\S+(?:" + _AUTH_PAIR_FOLD + r"\S+)?)"
)
# Shell/env-style assignments ("API_KEY=...", "token: ...") embedded in free
# text such as a command argument. The name is matched generically and then
# judged by is_secret_field_name, the same rule the field-name layer applies
# to a mapping key, so the two layers cannot drift apart over which names mean
# a credential — the free-text half used to carry its own shorter list and
# passed "Authorization=", "auth_token=", "credential=" and "MY_API_KEY="
# through untouched while the field-name half redacted every one of them.
# The name marker is descriptive and kept; only the assigned value goes.
_ASSIGNMENT_SECRET_RE = re.compile(r"(?<![\w.\-])([A-Za-z][\w.\-]{0,63})(\s*[:=]\s*)(\S+)")
# Punctuation that ends a sentence or a list item rather than belonging to the
# value, stripped before the value is judged and put back afterwards.
_VALUE_TRAILING_PUNCT = ",;.)]}\"'"
_NUMERIC_VALUE_RE = re.compile(r"^[-+]?\d+(?:[._]\d+)*$")


def _redact_auth_pair(match: re.Match[str]) -> str:
    name, separator, scheme, gap = match.group(1), match.group(2), match.group(3), match.group(4)
    if scheme:
        return f"{name}{separator}{scheme}{gap}[redacted]"
    return f"{name}{separator}[redacted]"


def _redact_assignment(match: re.Match[str]) -> str:
    name, separator, value = match.group(1), match.group(2), match.group(3)
    folded = fold_field_name(name)
    # Already handled with its scheme kept, by the pass above.
    if folded in _AUTH_PAIR_FIELD_NAMES or not is_secret_field_name(name):
        return match.group(0)
    core = value.rstrip(_VALUE_TRAILING_PUNCT)
    if _NUMERIC_VALUE_RE.match(core):
        # A count is not a credential, and the marker test matches by
        # substring, so "max_tokens" and "prompt_tokens" reach here. The
        # field-name layer already lets those through: redact_scalar only
        # redacts strings, so `"max_tokens": 4096` survives it, and the same
        # reading written out in free text has to survive this.
        return match.group(0)
    return f"{name}{separator}[redacted]{value[len(core) :]}"


_SECRET_VALUE_PREFIXES = ("sk-", "ghp_", "gho_", "ghu_", "ghs_", "xox", "AKIA", "eyJ")


def _leaf(match: re.Match[str]) -> str:
    raw = match.group(1)
    sep = "\\" if "\\" in raw else "/"
    return raw.rsplit(sep, 1)[-1] or "[redacted-path]"


# Complement to the shape-based patterns below: catches a secret with no
# recognizable shape by matching literal values from this process's own
# environment (which a Studio-launched run inherits). See
# docs/internals/studio.md ("Redaction"). Values under 4 characters are
# excluded as more likely incidental substring noise than a real secret.
_KNOWN_VALUE_MIN_LEN = 4


def known_secret_values() -> frozenset[str]:
    """Literal secret values read from this process's own environment --
    the config a Studio-launched run actually inherits."""
    values: set[str] = set()
    for key, value in os.environ.items():
        if not value or len(value) < _KNOWN_VALUE_MIN_LEN:
            continue
        if _is_secret_key(key):
            values.add(value)
    return frozenset(values)


def _scrub_known_values(text: str, known_values: frozenset[str]) -> str:
    if not text or not known_values:
        return text
    for value in known_values:
        if value in text:
            text = text.replace(value, "[redacted]")
    return text


def scrub_text(text: str, *, known_values: frozenset[str] | None = None) -> str:
    """Replace absolute-path-shaped and secret-token-shaped substrings
    embedded in free text. A leaf filename survives; the directory layout and
    the token itself do not.

    A ``name=value`` or ``name: value`` assignment is redacted whenever
    ``is_secret_field_name`` calls the name a credential, so free text and a
    mapping key are judged by one rule. An auth header keeps its scheme
    (``Authorization: Bearer [redacted]``) and a purely numeric value is left
    alone, since the marker test matches by substring and a token count is
    not a token.

    Also strips any literal value from ``known_values`` (default:
    `known_secret_values()`, this process's own env-derived secret values) --
    the complement to the shape-based patterns above, catching a genuine
    secret whose value does not happen to look like one.
    """
    if not text:
        return text
    text = _AUTH_PAIR_RE.sub(_redact_auth_pair, text)
    text = _ASSIGNMENT_SECRET_RE.sub(_redact_assignment, text)
    text = _BEARER_TOKEN_RE.sub("Bearer [redacted]", text)
    text = _ABS_POSIX_RE.sub(_leaf, text)
    text = _ABS_WIN_RE.sub(_leaf, text)
    text = _SECRET_TOKEN_RE.sub("[redacted]", text)
    text = _scrub_known_values(
        text, known_secret_values() if known_values is None else known_values
    )
    return text


def _is_secret_key(key: str) -> bool:
    return is_secret_field_name(key)


def _looks_like_secret_value(value: str) -> bool:
    if len(value) < 20:
        return False
    if _UUID_RE.match(value):
        return False
    if value.startswith(_SECRET_VALUE_PREFIXES):
        return True
    if any(ch.isspace() for ch in value):
        return False
    if not all(ch.isalnum() or ch in "-_." for ch in value):
        return False
    digits = sum(ch.isdigit() for ch in value)
    letters = len(value) - digits
    if digits == 0 or letters == 0:
        return False
    return len(value) >= 24


def redact_scalar(key: str, value: Any) -> Any:
    """Redact one scalar value found under ``key`` in a tool-call argument
    mapping (or a bare list item, when ``key`` is empty)."""
    if isinstance(value, str):
        if _is_secret_key(key) or _looks_like_secret_value(value):
            return "[redacted]"
        return scrub_text(value)[:PER_ITEM_TEXT_CAP]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"[{type(value).__name__}]"


def redact_arguments(value: Any, *, parent_key: str = "") -> Any:
    """Recursively redact secret- and absolute-path-shaped values, used on
    tool-call arguments and artifact contract/verification payloads.

    ``parent_key`` carries the field name a container arrived under, so a
    credential name covers what is nested beneath it, not only a scalar
    sitting directly on it -- see docs/internals/studio.md ("Redaction").
    """
    if isinstance(value, (dict, list)) and is_secret_field_name(parent_key):
        return "[redacted]"
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_key, val in value.items():
            key_name = str(raw_key)
            # Classification reads the key as the caller wrote it: scrubbing
            # first rewrites a path-shaped key down to its leaf, and a leaf
            # that no longer carries the credential marker would serve a value
            # the raw key withheld.
            redacted_val = (
                redact_arguments(val, parent_key=key_name)
                if isinstance(val, (dict, list))
                else redact_scalar(key_name, val)
            )
            # Keys are observable content too: a token or absolute host path
            # used as a JSON key must not escape merely because it is not in a
            # value position. Match _safe_content's mapping projection.
            safe_key = scrub_text(key_name)
            # scrub_text is not injective — distinct path-shaped keys can share
            # a leaf. Suffix instead of overwriting, so no entry silently
            # disappears from the projection.
            if safe_key in projected:
                ordinal = 2
                while f"{safe_key} [{ordinal}]" in projected:
                    ordinal += 1
                safe_key = f"{safe_key} [{ordinal}]"
            projected[safe_key] = redacted_val
        return projected
    if isinstance(value, list):
        return [
            redact_arguments(item) if isinstance(item, (dict, list)) else redact_scalar("", item)
            for item in value
        ]
    return redact_scalar("", value)


def cap_by_bytes(items: list[Any], limit: int = MESSAGE_BYTE_CAP) -> tuple[list[Any], bool]:
    """Keep the newest-first suffix of ``items`` whose JSON size stays under
    ``limit`` bytes. ``items`` is assumed chronological (oldest first); the
    return value preserves that order. Returns ``(kept, truncated)``.

    Fails closed on a single oversized item: earlier this admitted the
    newest item whole even when it alone exceeded ``limit`` (the aggregate
    check only ran once something had already been kept), which made the
    byte cap unsuitable as a bound. An oversized item is elided instead —
    scanning continues so a huge newest item does not also blank out smaller
    older ones.
    """
    kept_reversed: list[Any] = []
    total = 0
    truncated = False
    for item in reversed(items):
        size = len(json.dumps(item, default=str))
        if size > limit:
            truncated = True
            continue
        if total + size > limit:
            truncated = True
            break
        kept_reversed.append(item)
        total += size
    kept_reversed.reverse()
    return kept_reversed, truncated


def cap_payload_by_bytes(value: Any, limit: int = ARTIFACT_BYTE_CAP) -> tuple[Any, bool]:
    """Bound one already-redacted payload (not a list of items) to ``limit``
    bytes. Returns ``(value_or_placeholder, truncated)``. Used for artifact
    contract/verification projections, which are single JSON objects rather
    than a list ``cap_by_bytes`` can trim item by item."""
    if value is None:
        return None, False
    size = len(json.dumps(value, default=str))
    if size <= limit:
        return value, False
    return {"truncated": True, "reason": "exceeds the artifact byte cap"}, True
