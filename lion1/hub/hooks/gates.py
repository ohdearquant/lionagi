"""Before hooks as Python callables: the rules `scripts/` also carry, for an actor composed in code."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from lion1 import Actor, Run
from lion1.lndl import Lact

__all__ = ("SECRET_WORDS", "no_secrets")

SECRET_WORDS = (".env", "secret", "credential", "id_rsa", ".pem")


def no_secrets(
    actor: Actor, *, fields: Iterable[str] = ("path",), words: Iterable[str] = SECRET_WORDS
) -> None:
    """Refuse a command whose named string fields look like a secret's path."""
    fields, words = tuple(fields), tuple(w.lower() for w in words)

    def gate(run: Run, la: Lact, request: BaseModel) -> str | None:
        for field in fields:
            value = getattr(request, field, None)
            if isinstance(value, str) and any(w in value.lower() for w in words):
                return f"{value} looks like a secret; this actor does not read those"
        return None

    gate.__name__ = "no_secrets"
    actor.before(gate)
