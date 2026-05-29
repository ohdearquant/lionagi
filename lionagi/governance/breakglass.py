# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Break-glass emergency override mechanism for governance gates.

A BreakGlassSession allows an authorized actor to temporarily bypass
governance gate denials.  Every activation, override, and deactivation
is recorded to an EvidenceChain at the IMMUTABLE tier so the audit trail
cannot be silently deleted.

Typical lifecycle::

    session = BreakGlassSession(charter_break_glass=doc.break_glass)
    attestation = session.activate(
        attester_id="ops-lead-42",
        reason="prod outage mitigation, ticket INC-9999",
        duration_seconds=900,
        scope="deploy_tool",
    )
    # ... inside the hot-path ...
    result = session.check_override(gate_result)   # DENY → ALLOW while active
    # ... restore normal governance ...
    session.deactivate()
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from lionagi.governance.dsl import BreakGlassDef
from lionagi.governance.evidence import EvidenceChain, LogTier
from lionagi.governance.gates import GateResult, GateVerdict

__all__ = [
    "BreakGlassDisabledError",
    "BreakGlassInactiveError",
    "BreakGlassMissingAttestationError",
    "BreakGlassRecord",
    "BreakGlassSession",
]

from lionagi.governance.errors import (
    BreakGlassDisabledError,
    BreakGlassInactiveError,
    BreakGlassMissingAttestationError,
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class BreakGlassRecord(BaseModel):
    """Immutable record of a single break-glass activation window."""

    attester_id: str = Field(description="Identity of the actor who activated break-glass.")
    reason: str = Field(description="Human-readable justification for the override.")
    activated_at: datetime = Field(description="UTC timestamp of activation.")
    expires_at: datetime = Field(description="UTC timestamp of scheduled expiry.")
    scope: str = Field(
        default="all",
        description="Tool name or 'all'.  Overrides only apply when tool matches this scope.",
    )
    deactivated_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of early deactivation, if any.",
    )
    override_count: int = Field(
        default=0,
        description="Number of gate DENY verdicts converted to ALLOW during this window.",
    )

    def is_active(self) -> bool:
        """Return True if the window is currently open (not expired or deactivated)."""
        now = datetime.now(tz=timezone.utc)
        if self.deactivated_at is not None:
            return False
        return now < self.expires_at

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "attester_id": self.attester_id,
            "reason": self.reason,
            "activated_at": self.activated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "scope": self.scope,
            "deactivated_at": (self.deactivated_at.isoformat() if self.deactivated_at else None),
            "override_count": self.override_count,
        }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class BreakGlassSession:
    """Manages a single time-bounded governance bypass window.

    Thread-safety is provided by an internal :class:`threading.Lock`.  All
    mutations to ``_record`` are performed while the lock is held.

    Parameters
    ----------
    charter_break_glass:
        The ``BreakGlassDef`` from the compiled charter.  If ``None``, the
        session behaves as if break-glass is disabled.
    evidence_chain:
        Optional external :class:`EvidenceChain` to append events to.  If not
        provided, a new chain is created at construction time.
    """

    def __init__(
        self,
        charter_break_glass: BreakGlassDef | None = None,
        evidence_chain: EvidenceChain | None = None,
    ) -> None:
        self._charter = charter_break_glass
        self._chain: EvidenceChain = (
            evidence_chain if evidence_chain is not None else EvidenceChain()
        )
        self._record: BreakGlassRecord | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def evidence_chain(self) -> EvidenceChain:
        """The evidence chain recording all break-glass events."""
        return self._chain

    def activate(
        self,
        attester_id: str,
        reason: str,
        duration_seconds: int | None = None,
        scope: str = "all",
    ) -> BreakGlassRecord:
        """Open a break-glass override window.

        Parameters
        ----------
        attester_id:
            Non-empty string identifying who is activating break-glass.
        reason:
            Non-empty human-readable justification.
        duration_seconds:
            How long the window should remain open.  Defaults to the charter's
            ``max_duration_seconds`` (converted from ``expires_after``), or
            3 600 s when no charter is present.  Cannot exceed the charter limit.
        scope:
            Tool name to restrict overrides to, or ``"all"`` (default) to
            override all gates.

        Returns
        -------
        BreakGlassRecord
            The newly created activation record.

        Raises
        ------
        BreakGlassDisabledError
            When the charter has ``enabled: false``.
        BreakGlassMissingAttestationError
            When ``attester_id`` or ``reason`` is empty.
        ValueError
            When ``duration_seconds`` exceeds the charter maximum.
        """
        if not attester_id or not attester_id.strip():
            raise BreakGlassMissingAttestationError("attester_id must not be empty")
        if not reason or not reason.strip():
            raise BreakGlassMissingAttestationError("reason must not be empty")

        with self._lock:
            if self._charter is not None and not self._charter.enabled:
                raise BreakGlassDisabledError(
                    "Break-glass is disabled in the charter (enabled: false)"
                )

            max_seconds = self._charter_max_seconds()
            if duration_seconds is None:
                duration_seconds = max_seconds
            elif duration_seconds > max_seconds:
                raise ValueError(
                    f"Requested duration {duration_seconds}s exceeds charter maximum {max_seconds}s"
                )

            now = datetime.now(tz=timezone.utc)
            expires_at = datetime.fromtimestamp(now.timestamp() + duration_seconds, tz=timezone.utc)
            record = BreakGlassRecord(
                attester_id=attester_id,
                reason=reason,
                activated_at=now,
                expires_at=expires_at,
                scope=scope,
            )
            self._record = record

            self._chain.append(
                {
                    "event": "break_glass_activate",
                    **record.to_evidence_dict(),
                },
                tier=LogTier.IMMUTABLE,
            )

        return record

    def is_active(self) -> bool:
        """Return True if a break-glass window is currently open."""
        with self._lock:
            return self._record is not None and self._record.is_active()

    def deactivate(self) -> None:
        """Close the break-glass window early.

        Records a deactivation event with the final override count.  Calling
        this when no window is open is a no-op (idempotent).
        """
        with self._lock:
            if self._record is None or not self._record.is_active():
                return

            now = datetime.now(tz=timezone.utc)
            # BreakGlassRecord is a Pydantic model (not frozen), so we can update.
            self._record.deactivated_at = now

            self._chain.append(
                {
                    "event": "break_glass_deactivate",
                    **self._record.to_evidence_dict(),
                },
                tier=LogTier.IMMUTABLE,
            )

    def check_override(self, gate_result: GateResult) -> GateResult:
        """Convert a DENY verdict to ALLOW when a valid break-glass window is open.

        If break-glass is inactive or the scope does not match the gate's
        ``gate_id`` (used as a proxy for tool name in tests where the GateResult
        is standalone), the original ``gate_result`` is returned unchanged.

        For ALLOW / ADVISORY verdicts the result is also returned unchanged —
        there is nothing to override.

        Parameters
        ----------
        gate_result:
            The :class:`GateResult` returned by a gate evaluation.

        Returns
        -------
        GateResult
            Either the original result or a new ALLOW result annotated with the
            break-glass justification.
        """
        if gate_result.verdict != GateVerdict.DENY:
            return gate_result

        with self._lock:
            if self._record is None or not self._record.is_active():
                return gate_result

            scope = self._record.scope
            tool_name = gate_result.gate_id  # gate_id is used as the tool/gate identifier

            if scope != "all" and scope != tool_name:
                return gate_result

            # Increment override counter (mutable field on the record model)
            self._record.override_count += 1

            overridden = GateResult(
                verdict=GateVerdict.ALLOW,
                justification=(
                    f"[BREAK-GLASS] Override by {self._record.attester_id}: "
                    f"{self._record.reason} "
                    f"(original denial: {gate_result.justification})"
                ),
                gate_id=gate_result.gate_id,
                policy_ref=gate_result.policy_ref,
                evidence_ref=gate_result.evidence_ref,
                elapsed_ms=gate_result.elapsed_ms,
            )

            self._chain.append(
                {
                    "event": "break_glass_override",
                    "gate_id": gate_result.gate_id,
                    "original_verdict": gate_result.verdict.value,
                    "original_justification": gate_result.justification,
                    "attester_id": self._record.attester_id,
                    "scope": self._record.scope,
                    "override_count": self._record.override_count,
                },
                tier=LogTier.IMMUTABLE,
            )

        return overridden

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _charter_max_seconds(self) -> int:
        """Derive the maximum allowed duration from the charter definition."""
        if self._charter is None:
            return 3600

        raw = self._charter.expires_after  # e.g. "15m" or "1h"
        amount = int(raw[:-1])
        unit = raw[-1]
        if unit == "h":
            return amount * 3600
        return amount * 60  # "m"
