# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tri-state turn-origin disposition threaded through the chat/run operation context.

A model-submission turn is either genuinely user-originated (a public ingress
called with no upstream instruction) or purely internal (a repair retry, a
ReAct extension round, ...). Distinguishing the two lets a single blocking
hook point fire exactly once per user turn, no matter how many internal calls
that turn triggers underneath it.

Three explicit states, carried as a field on the operation context (never
ambient/task-local, since concurrent branch operations must not leak state
into each other):

- ``unset`` — the default a genuine outside caller produces. The model-
  submission boundary mints a fresh token and fires.
- ``forwarded`` — an already-minted token, carried through unchanged. Never
  re-originated; a caller that receives a forwarded disposition must pass it
  on as-is, not re-mint.
- ``no-origin`` — the call traverses without ever holding a token. The
  boundary stays silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

__all__ = ("TurnOrigin", "resolve_turn_origin", "consume_turn_origin")

_UNSET = "unset"
_FORWARDED = "forwarded"
_NO_ORIGIN = "no-origin"


@dataclass(slots=True, frozen=True)
class TurnOrigin:
    """A disposition (``unset`` | ``forwarded`` | ``no-origin``) plus its token, if any."""

    disposition: str
    token: str | None = None

    @classmethod
    def unset(cls) -> TurnOrigin:
        return cls(_UNSET, None)

    @classmethod
    def no_origin(cls) -> TurnOrigin:
        return cls(_NO_ORIGIN, None)

    @classmethod
    def forwarded(cls, token: str) -> TurnOrigin:
        if not token:
            raise ValueError("a forwarded TurnOrigin requires a non-empty token")
        return cls(_FORWARDED, token)

    def mint_if_unset(self) -> TurnOrigin:
        """Public-ingress rule: mint a fresh token only when the disposition is unset."""
        if self.disposition == _UNSET:
            return TurnOrigin.forwarded(uuid4().hex)
        return self


def resolve_turn_origin(raw: Any) -> TurnOrigin:
    """Normalize an operation-context value to a ``TurnOrigin``, defaulting to unset.

    ``raw`` is whatever a ``ChatParam``/``RunParam`` ``turn_origin`` field holds,
    which may be a real ``TurnOrigin``, ``None``, or the ``Unset``/``Undefined``
    sentinel a caller never touched — all non-``TurnOrigin`` values collapse to
    ``unset()``.
    """
    if isinstance(raw, TurnOrigin):
        return raw
    return TurnOrigin.unset()


def consume_turn_origin(raw: Any) -> str | None:
    """Model-submission-boundary rule: mint-if-unset, then return the active token.

    Returns ``None`` when the resolved disposition is ``no-origin`` — the
    boundary stays silent in that case; otherwise returns the token to fire
    ``USER_PROMPT_SUBMIT`` with.
    """
    resolved = resolve_turn_origin(raw).mint_if_unset()
    return resolved.token if resolved.disposition == _FORWARDED else None
