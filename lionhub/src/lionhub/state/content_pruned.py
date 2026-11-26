# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The marker a reclaimed message body leaves in its place.

Emptying a message's content to free space risks losing not the text but the
fact that text was ever there: an empty string or ``{}`` is exactly what a
turn that genuinely produced nothing writes, so a reader can't tell the two
apart once collapsed. A reclaimed body is therefore never emptied -- it's
replaced by a marker that says what it is and what used to be there. This
module is the vocabulary both sides use: the writer is ``li state
null-content``; the readers are whatever displays or counts message bodies.
Kept out of ``db.py`` so asking the question costs a reader nothing but this
import. The marker records ``at`` and ``original_bytes`` -- the size of the
body that specific row held, not an average over a batch (see
``pruned_content_sql``).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "CONTENT_PRUNED_KEY",
    "pruned_content_sql",
    "content_was_pruned",
]

# The single key a reclaimed body carries. Prefixed rather than named something
# like "pruned" because it shares a namespace with every content shape lionagi
# writes -- instruction, assistant_response, function/arguments, and whatever a
# future message type introduces -- and a collision would make a real body read
# as reclaimed.
CONTENT_PRUNED_KEY = "lion_content_pruned"


def pruned_content_sql(*, at_param: str = "at", size_expr: str = "LENGTH(content)") -> str:
    """The marker as a SQL expression, evaluated per row against the row it replaces.

    The size is built from a SQL expression over the row being replaced,
    rather than computed once in Python and reused, which is the whole
    reason this returns SQL rather than a dict: the database reads each old
    body's length while overwriting it, in one statement, so
    ``original_bytes`` is genuinely the row's own size and not a batch
    average wearing a per-row name. ``size_expr`` is a caller-supplied SQL
    fragment; the default is the only production use.
    """
    return (
        f"json_object('{CONTENT_PRUNED_KEY}', "
        f"json_object('at', :{at_param}, 'original_bytes', {size_expr}))"
    )


def content_was_pruned(content: Any) -> bool:
    """True when this body was reclaimed, as opposed to having been empty.

    Takes the column either raw (JSON text as SQLite stores it) or hydrated
    (a dict a reader already parsed), since those reach consumers by
    different routes. Anything unparseable is treated as not reclaimed; this
    says nothing about whether the body is well-formed, it answers one
    question only.
    """
    if isinstance(content, str):
        # Cheap substring test before parsing -- a marker is a few dozen
        # bytes, a body can be megabytes. Only ever short-circuits to False,
        # so a body that merely mentions the key still gets parsed below.
        if CONTENT_PRUNED_KEY not in content:
            return False
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
    return isinstance(content, dict) and CONTENT_PRUNED_KEY in content
