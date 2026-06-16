# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Guards for operation entry points — reject removed kwargs that would otherwise be silently forwarded."""

from __future__ import annotations


def reject_removed_kwargs(kwargs: dict, removed: dict[str, str], *, where: str) -> None:
    """Raise ``TypeError`` if any removed parameter appears in ``kwargs``; ``removed`` maps name → replacement hint."""
    hit = [name for name in removed if name in kwargs]
    if hit:
        detail = ", ".join(
            f"{name!r} (use {removed[name]})" if removed[name] else repr(name) for name in hit
        )
        raise TypeError(f"{where}() no longer accepts {detail}")
