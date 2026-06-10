# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Guards for operation entry points that collect ``**kwargs``.

Operation helpers forward unrecognized keyword arguments to the provider as
``imodel_kw``. That catch-all means a parameter removed from the explicit
signature is no longer an error — it is silently packed into the outgoing
payload (or dropped), changing behavior with no signal. ``reject_removed_kwargs``
restores a loud, actionable failure for names we have intentionally removed.
"""

from __future__ import annotations


def reject_removed_kwargs(kwargs: dict, removed: dict[str, str], *, where: str) -> None:
    """Raise ``TypeError`` if any removed parameter name appears in ``kwargs``.

    Args:
        kwargs: The catch-all keyword mapping about to be forwarded.
        removed: Maps each removed name to a short replacement hint shown in
            the error (e.g. ``{"request_model": "response_format="}``). An
            empty hint means the parameter was removed with no replacement.
        where: Caller name used in the error message.
    """
    hit = [name for name in removed if name in kwargs]
    if hit:
        detail = ", ".join(
            f"{name!r} (use {removed[name]})" if removed[name] else repr(name) for name in hit
        )
        raise TypeError(f"{where}() no longer accepts {detail}")
