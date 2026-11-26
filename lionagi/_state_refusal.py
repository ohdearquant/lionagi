# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The refusal a compatibility-mode state store raises, and how to find it in a failure.

Operations, the message manager, the hook bus and the session observer check a failure for this
refusal. They import it from here, a module with no store dependencies, so those checks do not
load the store. ``lionhub.state.compat`` re-exports both names, and the class is the same object
under either path.
"""

from __future__ import annotations

from lionagi.ln.concurrency._compat import BaseExceptionGroup

__all__ = (
    "StateCompatibilityRefused",
    "refusal_in",
)


class StateCompatibilityRefused(RuntimeError):  # noqa: N818 -- the name ADR-0128 D6 gives it
    """A compatibility-mode StateDB refused an operation.

    ``committed`` names the transactions of the refused operation that had already committed when
    it was refused. Empty means the refused operation left the store's contents unchanged.
    """

    def __init__(
        self, operation: str, profile: str, reason: str, committed: tuple[str, ...] = ()
    ) -> None:
        self.operation = operation
        self.profile = profile
        self.reason = reason
        self.committed = tuple(committed)
        outcome = (
            f"already committed: {', '.join(self.committed)}"
            if self.committed
            else "nothing was committed"
        )
        super().__init__(
            f"{operation} refused in compatibility mode (profile {profile}): {reason}; {outcome}"
        )

    def __reduce__(self):
        return (type(self), (self.operation, self.profile, self.reason, self.committed))


def refusal_in(error: BaseException | None) -> StateCompatibilityRefused | None:
    """The store refusal *error* is, or carries inside exception groups however deeply nested.

    Task groups and callback collectors wrap failures, so a refusal can arrive beside a
    cancellation or another error; a plain ``isinstance`` on the outermost exception would read
    that group as an ordinary failure.
    """
    if isinstance(error, StateCompatibilityRefused):
        return error
    if isinstance(error, BaseExceptionGroup):
        for item in error.exceptions:
            found = refusal_in(item)
            if found is not None:
                return found
    return None
