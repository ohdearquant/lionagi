# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The marker a reclaimed message body leaves in its place.

Freeing space by emptying a message's content has one failure mode that
matters, and it is not losing the text. It is losing the fact that text was
ever there. A body written as an empty string, or as ``{}``, is exactly what a
turn that genuinely produced nothing writes, so a reader handed one cannot say
which it is looking at, and neither can anything above it: nothing downstream
has a second source for that distinction, so the two collapse into one state
permanently and silently.

A reclaimed body is therefore not emptied. It is replaced by a value that says
what it is and what used to be there, and this module is the vocabulary both
sides use. The writer is ``li state null-content``; the readers are whatever
displays or counts message bodies. Kept out of ``db.py`` so asking the question
costs a reader nothing but this import.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "CONTENT_PRUNED_KEY",
    "pruned_content",
    "content_was_pruned",
]

# The single key a reclaimed body carries. Prefixed rather than named something
# like "pruned" because it shares a namespace with every content shape lionagi
# writes -- instruction, assistant_response, function/arguments, and whatever a
# future message type introduces -- and a collision would make a real body read
# as reclaimed.
CONTENT_PRUNED_KEY = "lion_content_pruned"


def pruned_content(*, at: float, original_bytes: int) -> dict[str, Any]:
    """The value a reclaimed message body is replaced with.

    Carries when the reclaim happened and how large the body was, because a
    marker that only says "gone" cannot answer the question the reclaim was
    performed to answer: how much the operation actually recovered, after the
    fact, from the store itself rather than from a number a command printed
    once.
    """
    return {CONTENT_PRUNED_KEY: {"at": at, "original_bytes": original_bytes}}


def content_was_pruned(content: Any) -> bool:
    """True when this body was reclaimed, as opposed to having been empty.

    Takes the column either raw (the JSON text SQLite stores) or hydrated (the
    dict a reader has already parsed), because those reach consumers by
    different routes and a predicate that only served one of them would answer
    "no" for the other -- which is the same wrong answer as having no marker at
    all, arrived at more expensively.

    Anything unparseable is not reclaimed. This says nothing about the body
    being well-formed; it answers one question only.
    """
    if isinstance(content, str):
        # A marker is a few dozen bytes and a body can be megabytes. The
        # substring test is what keeps this from parsing every row it is handed
        # -- and it only ever short-circuits to False, so a body that merely
        # mentions the key still goes through the parse below and is judged on
        # its structure rather than on its text.
        if CONTENT_PRUNED_KEY not in content:
            return False
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
    return isinstance(content, dict) and CONTENT_PRUNED_KEY in content
