# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared credential field-name classification.

Redaction callers must agree about which spellings identify a credential.
Keeping the separator fold and vocabulary here prevents an adapter error path
from exposing a field that Studio's richer projections correctly withhold.
"""

from __future__ import annotations

import re

__all__ = (
    "EXACT_SECRET_FIELD_NAMES",
    "SECRET_KEY_MARKERS",
    "fold_field_name",
    "is_secret_field_name",
)

# The marker vocabulary includes separated and run-together spellings. The
# predicate below owns their matching rules; not every marker is a substring.
# Field-name folding normalizes punctuation but intentionally does not guess
# camel-case boundaries, so ``accessKey`` folds to ``accesskey`` while
# ``access-key`` folds to ``access_key``.
SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "auth_token",
    "access_key",
    "accesskey",
    "private_key",
    "privatekey",
    "hmac_key",
    "hmackey",
    "signing_key",
    "signingkey",
    "client_secret",
    "bearer",
)

# Names that mean a credential on their own but must not match as substrings.
# "auth" inside "author" or "authorized_keys_count" is a different word.
EXACT_SECRET_FIELD_NAMES = frozenset({"auth", "authentication", "bearer"})

_FIELD_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
# The new key-material markers need word boundaries so ``designing_key``,
# ``signing_keyword`` and ``hmac_keyboard`` do not identify credentials.
_SIGNING_KEY_MARKERS = frozenset({"hmac_key", "hmackey", "signing_key", "signingkey"})
_CAMEL_WORD_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SIGNING_KEY_RE = re.compile(r"(?:^|_)(?:hmac_?key|signing_?key)(?:_|$)")


def fold_field_name(key: str) -> str:
    """Normalize case and punctuation without guessing camel-case words.

    Multi-word markers include both separated and run-together forms, so this
    also recognizes ``access_key``, ``access-key``, and ``accessKey`` while
    avoiding broad terms such as ``access`` or ``private`` on their own.
    """
    return _FIELD_SEPARATOR_RE.sub("_", key.lower())


def is_secret_field_name(key: str) -> bool:
    """Return whether ``key`` identifies a credential-bearing field."""
    folded = fold_field_name(key)
    signing_words = fold_field_name(_CAMEL_WORD_RE.sub("_", key))
    return (
        folded in EXACT_SECRET_FIELD_NAMES
        or bool(_SIGNING_KEY_RE.search(signing_words))
        or any(
            marker in folded for marker in SECRET_KEY_MARKERS if marker not in _SIGNING_KEY_MARKERS
        )
    )
