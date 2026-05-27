# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Just-in-time (JIT) tool grant: temporary single-use permits for privileged tool calls.

ADR-0046: JIT Tool Grant.

A JIT grant is a no-standing-capability, time-bounded permit that allows a
specific agent role to invoke a normally-denied tool exactly once (or up to
``max_uses`` times).  Permits are issued by an authorized grantor, consumed
atomically when the tool is invoked, and can be revoked before consumption.

Public surface
--------------
PermitToken       — immutable record of a single grant
JITGrantStore     — thread-safe in-memory store for active permits
check_jit_grant   — find and consume a matching permit for (tool_id, role)
jit_gate_override — convert a DENY GateResult to ALLOW when a valid permit exists
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lionagi.protocols.governance.gates import GateResult, GateVerdict

__all__ = [
    "PermitToken",
    "JITGrantStore",
    "check_jit_grant",
    "jit_gate_override",
]

# ---------------------------------------------------------------------------
# PermitToken
# ---------------------------------------------------------------------------

PermitScope = Literal["session", "flow", "global"]


class PermitToken(BaseModel):
    """Immutable record of a single JIT grant permit.

    Fields
    ------
    token_id        : Unique UUID for this permit.
    tool_id         : The tool that this permit authorises.
    grantee_role    : The role the permit was issued to.
    grantor         : Identifier of the issuing authority.
    reason          : Human-readable justification for the grant.
    max_uses        : Maximum number of times the permit may be consumed.
    uses_remaining  : How many consumptions remain (decremented on each consume).
    created_at      : Unix timestamp (float) of issuance.
    expires_at      : Unix timestamp (float) after which the permit is invalid.
    scope           : Permit scope — "session", "flow", or "global".
    revoked         : True once the permit has been explicitly revoked.
    """

    model_config = ConfigDict(frozen=True)

    token_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool_id: str
    grantee_role: str
    grantor: str
    reason: str
    max_uses: int = Field(default=1, ge=1)
    uses_remaining: int = Field(default=1, ge=0)
    created_at: float = Field(default_factory=time.time)
    expires_at: float
    scope: PermitScope = "session"
    revoked: bool = False

    def is_active(self, now: float | None = None) -> bool:
        """Return True if the permit is not expired, not revoked, and has uses remaining."""
        t = now if now is not None else time.time()
        return not self.revoked and self.uses_remaining > 0 and t < self.expires_at

    def with_uses_remaining(self, n: int) -> PermitToken:
        """Return a copy of this token with uses_remaining set to *n*."""
        return self.model_copy(update={"uses_remaining": n})

    def revoke(self) -> PermitToken:
        """Return a revoked copy of this token."""
        return self.model_copy(update={"revoked": True})


# ---------------------------------------------------------------------------
# JITGrantStore
# ---------------------------------------------------------------------------


class JITGrantStore:
    """Thread-safe in-memory store for active JIT permit tokens.

    All mutating operations are protected by a single ``threading.Lock`` to
    ensure that concurrent consume calls for a single-use token cannot both
    succeed.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        # token_id -> PermitToken (authoritative copy)
        self._tokens: dict[str, PermitToken] = {}

    # ------------------------------------------------------------------
    # Issuance
    # ------------------------------------------------------------------

    def issue(
        self,
        tool_id: str,
        grantee_role: str,
        grantor: str,
        reason: str,
        max_uses: int = 1,
        ttl_seconds: float = 300.0,
        scope: PermitScope = "session",
    ) -> PermitToken:
        """Issue a new JIT permit and store it.

        Parameters
        ----------
        tool_id       : Identifier of the tool being permitted.
        grantee_role  : Role that may consume this permit.
        grantor       : Identifier of the issuing authority.
        reason        : Justification for the grant.
        max_uses      : Number of consumptions allowed (default 1).
        ttl_seconds   : Lifetime of the permit in seconds (default 300).
        scope         : Permit scope — "session", "flow", or "global".

        Returns
        -------
        The newly issued PermitToken.
        """
        now = time.time()
        token = PermitToken(
            tool_id=tool_id,
            grantee_role=grantee_role,
            grantor=grantor,
            reason=reason,
            max_uses=max_uses,
            uses_remaining=max_uses,
            created_at=now,
            expires_at=now + ttl_seconds,
            scope=scope,
            revoked=False,
        )
        with self._lock:
            self._tokens[token.token_id] = token
        return token

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------

    def consume(self, token_id: str, tool_id: str, role: str) -> bool:
        """Attempt to consume one use of the permit.

        The consume is atomic: under the lock we check validity, decrement
        ``uses_remaining``, and write the updated token back.  This prevents
        two concurrent callers from both consuming a single-use token.

        Parameters
        ----------
        token_id : Token to consume.
        tool_id  : Must match the token's ``tool_id``.
        role     : Must match the token's ``grantee_role``.

        Returns
        -------
        True if the permit was valid and has been decremented; False otherwise.
        """
        now = time.time()
        with self._lock:
            token = self._tokens.get(token_id)
            if token is None:
                return False
            if token.tool_id != tool_id:
                return False
            if token.grantee_role != role:
                return False
            if not token.is_active(now):
                return False
            updated = token.with_uses_remaining(token.uses_remaining - 1)
            self._tokens[token_id] = updated
            return True

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    def revoke(self, token_id: str) -> bool:
        """Mark a permit as revoked.

        Parameters
        ----------
        token_id : Token to revoke.

        Returns
        -------
        True if the token existed and was not already revoked; False otherwise.
        """
        with self._lock:
            token = self._tokens.get(token_id)
            if token is None:
                return False
            if token.revoked:
                return False
            self._tokens[token_id] = token.revoke()
            return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_active(
        self,
        role: str | None = None,
        tool_id: str | None = None,
    ) -> list[PermitToken]:
        """Return all currently active tokens, optionally filtered.

        Parameters
        ----------
        role    : If given, restrict to tokens with this ``grantee_role``.
        tool_id : If given, restrict to tokens with this ``tool_id``.

        Returns
        -------
        List of active PermitToken objects (snapshot under the lock).
        """
        now = time.time()
        with self._lock:
            tokens = list(self._tokens.values())

        result: list[PermitToken] = []
        for token in tokens:
            if not token.is_active(now):
                continue
            if role is not None and token.grantee_role != role:
                continue
            if tool_id is not None and token.tool_id != tool_id:
                continue
            result.append(token)
        return result

    def cleanup_expired(self) -> int:
        """Remove expired tokens from the store.

        Returns
        -------
        Number of tokens removed.
        """
        now = time.time()
        with self._lock:
            expired_ids = [tid for tid, token in self._tokens.items() if token.expires_at <= now]
            for tid in expired_ids:
                del self._tokens[tid]
        return len(expired_ids)

    # ------------------------------------------------------------------
    # Internal helpers (for testing)
    # ------------------------------------------------------------------

    def get(self, token_id: str) -> PermitToken | None:
        """Return the stored token by ID, or None if not found."""
        with self._lock:
            return self._tokens.get(token_id)


# ---------------------------------------------------------------------------
# check_jit_grant
# ---------------------------------------------------------------------------


def check_jit_grant(
    store: JITGrantStore,
    tool_id: str,
    role: str,
) -> PermitToken | None:
    """Find the first active permit for *(tool_id, role)* and consume it.

    Iterates active tokens filtered by tool and role.  Returns the first
    token whose ``consume`` succeeds, or None when no valid token exists.

    Parameters
    ----------
    store   : The JITGrantStore to search.
    tool_id : Tool being requested.
    role    : Role of the requesting agent.

    Returns
    -------
    The consumed PermitToken on success, or None.
    """
    candidates = store.list_active(role=role, tool_id=tool_id)
    for token in candidates:
        if store.consume(token.token_id, tool_id, role):
            # Return the pre-consume snapshot so callers can inspect the grant
            return token
    return None


# ---------------------------------------------------------------------------
# jit_gate_override
# ---------------------------------------------------------------------------


def jit_gate_override(
    store: JITGrantStore,
    gate_result: GateResult,
    tool_id: str,
    role: str,
) -> GateResult:
    """Convert a DENY GateResult to ALLOW if a valid JIT grant exists.

    If *gate_result* is already ALLOW or ADVISORY, it is returned unchanged.
    If *gate_result* is DENY, the store is searched for a matching active
    permit.  If found, the permit is consumed and a new ALLOW GateResult is
    returned whose justification records the grant token ID.

    Parameters
    ----------
    store       : Active JITGrantStore to query.
    gate_result : The result produced by normal gate evaluation.
    tool_id     : Tool being evaluated.
    role        : Role of the requesting agent.

    Returns
    -------
    The original GateResult, or a new ALLOW GateResult when a grant overrides
    a DENY verdict.
    """
    if gate_result.verdict != GateVerdict.DENY:
        return gate_result

    token = check_jit_grant(store, tool_id, role)
    if token is None:
        return gate_result

    return GateResult(
        verdict=GateVerdict.ALLOW,
        justification=(
            f"JIT grant override: permit {token.token_id!r} issued by {token.grantor!r} "
            f"for role {token.grantee_role!r} — {token.reason}"
        ),
        gate_id=gate_result.gate_id,
        policy_ref=gate_result.policy_ref,
        evidence_ref=token.token_id,
        elapsed_ms=gate_result.elapsed_ms,
    )
