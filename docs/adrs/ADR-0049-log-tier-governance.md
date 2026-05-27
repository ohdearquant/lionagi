# ADR-0049: Log Tier Governance

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md), [ADR-0045](ADR-0045-break-glass-protocol.md)
**Related**: [ADR-0042](ADR-0042-task-certificate.md), [ADR-0044](ADR-0044-tool-gates.md), [ADR-0046](ADR-0046-jit-tool-grant.md), [ADR-0047](ADR-0047-agent-charter.md), [ADR-0050](ADR-0050-operation-context.md)

## Context

`DataLogger` in lionagi today writes `Log` entries to JSONL, JSON, or CSV files with no tier
distinction. Every entry — a `log.debug.*` timing trace, a `branch.add_message` event, an evidence
emission — goes into the same flat `Pile[Log]`, subject to the same `dump()` and `clear()`
operations. An agent that gains write access to its own session logs can erase the record of what
it did. More subtly, a compromised agent or injected tool could call `DataLogger.dump(clear=True)`
and silently discard the evidence of a gate decision or charter violation before the session ends.

Cross-cutting principle #2 states: **evidence is first-class, not logs**. Evidence carries the
policy version and gate results active at execution time ("active assertion"); operational logs are
supporting context. But both flow through the same `DataLogger` today, and the substrate makes no
distinction. This means the substrate that should protect governance-critical records is the same
substrate that rotates debug traces. That conflation is the gap this ADR closes.

The triggering scenario: an attacker model where an insider or compromised agent disables logging
before acting. In lionagi this manifests as: (1) a tool call that invokes `DataLogger.dump(clear=True)`
immediately before an unauthorized action, erasing the tool-invocation log; or (2) a session
teardown that discards all in-memory logs before the evidence nodes from ADR-0041 are flushed to
the backend. Without tier distinction, the application has no way to treat these two operations
differently.

### The applicable prior governance research insight

prior research introduces a three-tier log hierarchy — MUTABLE / PROTECTED / IMMUTABLE — where
each tier has distinct deletion semantics enforced at both the application layer and the database
layer. The core insight is that "audit of auditing" closes the disable-logging attack vector: any
deletion of a PROTECTED log must itself create an IMMUTABLE evidence node recording who deleted
what and when. An attacker that disables logging cannot hide the act of disabling. In lionagi terms:
the `DataLogger` becomes tier-aware, and the tier of a `Log` entry is set at creation time by the
code path that emits it, not by post-hoc classification.

### Why lionagi needs this

Without tier governance, a test run, a debug session, and a gate decision all produce logs with
identical tamperability. Consider: `branch.operate()` triggers a HARD gate (ADR-0044); the gate
approves with an evidence emission (ADR-0041); the agent's tool then calls a file-deletion
operation. That gate decision is the audit trail. If the same agent can call `DataLogger.clear()`,
the gate decision disappears before it is persisted. With `LogTier.IMMUTABLE`, the `DataLogger`
rejects any mutation of that entry — not by policy document, but by code path enforcement, which
satisfies cross-cutting principle #3: every constraint must be enforced, not just documented.

## Decision

We introduce a `LogTier` enum and make `DataLogger` tier-aware: MUTABLE entries can be deleted
freely, PROTECTED entries require a break-glass token per ADR-0045 and their deletion creates an
IMMUTABLE evidence node, and IMMUTABLE entries have no deletion API.

### 1. LogTier enum and Log extension

```python
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import PrivateAttr

from lionagi.models.hashable_model import HashableModel
from lionagi.protocols.generic.element import Element
from lionagi.utils import to_dict


# lionagi/protocols/generic/log.py
# Extend the existing module in place; add "LogTier" to __all__.


class LogTier(str, Enum):
    """Three-tier log governance hierarchy.

    Tier assignment is set at creation time by the emitting code path.
    Default is MUTABLE; higher tiers require explicit opt-in.

    MUTABLE:   debug, performance, ephemeral traces. Freely rotated.
    PROTECTED: session activity, tool calls, status transitions, errors.
               Deletion requires break-glass (ADR-0045); deletion itself
               creates an IMMUTABLE evidence node.
    IMMUTABLE: evidence emissions (ADR-0041), task certificate state changes
               (ADR-0042), gate decisions (ADR-0044), break-glass attestations
               (ADR-0045), permit issuance/consumption (ADR-0046), charter
               activations (ADR-0047), operation context captures (ADR-0050).
               No deletion API exists. Write-once at the application layer;
               DB-level triggers in KHive backend.
    """

    MUTABLE = "mutable"
    PROTECTED = "protected"
    IMMUTABLE = "immutable"


class Log(Element):
    """Existing Log entry, extended in place with explicit tier governance.

    The `tier` field defaults to MUTABLE. Emitters that produce
    governance-critical records must set tier explicitly:

        Log.create(content, tier=LogTier.IMMUTABLE)

    Once a Log with tier=IMMUTABLE is created, no mutation is permitted.
    Once a Log with tier=PROTECTED is created, deletion requires a
    break-glass token from ADR-0045.
    """

    content: dict[str, Any]
    tier: LogTier = LogTier.MUTABLE
    _immutable: bool = PrivateAttr(False)

    def __setattr__(self, name: str, value: Any) -> None:
        """Reuse the existing PrivateAttr immutability mechanism."""
        if getattr(self, "_immutable", False):
            raise AttributeError("This Log is immutable.")
        super().__setattr__(name, value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Log":
        """Restore a Log and preserve the existing read-only restore behavior."""
        self = cls.model_validate(data)
        self._immutable = True
        return self

    @classmethod
    def create(
        cls,
        content: "Element | dict[str, Any]",
        tier: LogTier = LogTier.MUTABLE,
    ) -> "Log":
        """Create a Log entry with an explicit tier.

        Args:
            content: The payload. Element instances are serialized via
                     ``to_dict(mode='json')``.
            tier:    Governance tier. Defaults to MUTABLE; callers emitting
                     evidence, certificates, or gate decisions must pass
                     tier=LogTier.IMMUTABLE explicitly.
        """
        if isinstance(content, Element | HashableModel):
            payload = content.to_dict(mode="json")
        else:
            payload = to_dict(content, recursive=True, suppress=True)

        if not payload:
            payload = {"error": "No content to log."}

        log = cls(content=payload, tier=tier)
        if tier == LogTier.IMMUTABLE:
            log._immutable = True
        return log
```

### 2. Tier-aware DataLogger operations

```python
from __future__ import annotations

import atexit
import json
import logging
from pathlib import Path
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

from .log import DataLoggerConfig, Log, LogTier

_log = logging.getLogger(__name__)


class ProtectedLogDeletion(Element):
    """Governance metadata emitted as an IMMUTABLE Log via DataLogger."""

    deleted_log_id: str
    deleted_log_tier: LogTier = Field(default=LogTier.PROTECTED)
    break_glass_token_id: str
    actor: str
    reason: str
    deleted_content_hash: str


class DataLogger:
    """Existing DataLogger, extended in place as the tier-aware log manager.

    Three entry points for deletion, each enforcing a different tier contract:

    - ``delete_log(log_id)`` - MUTABLE only; raises for higher tiers.
    - ``delete_protected_logs(log_ids, ...)`` - PROTECTED only; requires break-glass.
    - There is no ``delete_immutable_log``. The method does not exist.

    ``dump()`` writes IMMUTABLE entries to a separate file (``*_immutable.*``)
    that the KHive backend opens in append-only mode. MUTABLE and PROTECTED
    entries go to the standard file.
    """

    def __init__(
        self,
        *,
        logs: Any = None,
        _config: DataLoggerConfig = None,
        **kwargs,
    ):
        if _config is None:
            _config = DataLoggerConfig(**kwargs)

        if isinstance(logs, dict):
            self.logs = Pile.from_dict(logs)
        else:
            self.logs = Pile(collections=logs, item_type=Log, strict_type=True)
        self._config = _config

        if self._config.auto_save_on_exit:
            atexit.register(self.save_at_exit)

    def log(self, log_: Any, *, tier: LogTier = LogTier.MUTABLE) -> None:
        """Add a log synchronously, preserving DataLogger as the integration point."""
        log_ = Log.create(log_, tier=tier) if not isinstance(log_, Log) else log_
        if self._config.capacity and len(self.logs) >= self._config.capacity:
            self.dump(clear=self._config.clear_after_dump)
        self.logs.include(log_)

    async def alog(self, log_: Any, *, tier: LogTier = LogTier.MUTABLE) -> None:
        """Add a log asynchronously."""
        async with self.logs:
            self.log(log_, tier=tier)

    def delete_log(self, log_id: str | UUID) -> None:
        """Delete a MUTABLE log entry by ID.

        Raises:
            PermissionError: If the entry is PROTECTED or IMMUTABLE.
            KeyError:        If no entry with ``log_id`` exists.
        """
        entry: Log = self.logs[log_id]  # raises KeyError if absent
        if entry.tier == LogTier.PROTECTED:
            raise PermissionError(
                f"Log {log_id} has tier=PROTECTED. "
                "Use delete_protected_logs() with a valid break-glass token (ADR-0045)."
            )
        if entry.tier == LogTier.IMMUTABLE:
            raise PermissionError(
                f"Log {log_id} has tier=IMMUTABLE. "
                "Deletion is not permitted. No API exists for IMMUTABLE deletion."
            )
        self.logs.pop(entry.id)

    def delete_protected_logs(
        self,
        log_ids: str | UUID | list[str | UUID],
        break_glass_token: str,
        reason: str,
    ) -> None:
        """Delete PROTECTED log entries under break-glass authorization.

        The act of deletion itself creates an IMMUTABLE evidence node
        ("audit of auditing"). An attacker that deletes a PROTECTED log
        cannot hide that the deletion occurred.

        Args:
            log_ids:           ID or IDs of the PROTECTED logs to delete.
            break_glass_token: Valid token issued by ADR-0045 protocol.
            reason:            Human-readable justification, recorded
                               in the IMMUTABLE evidence node.

        Raises:
            PermissionError: If entry is MUTABLE (use delete_log) or
                             IMMUTABLE (no deletion permitted), or if
                             break_glass_token fails validation.
            KeyError:        If no entry with ``log_id`` exists.
        """
        from lionagi.protocols.governance.break_glass import validate_break_glass_token

        # Validate break-glass token — raises if invalid (ADR-0045)
        attestation = validate_break_glass_token(break_glass_token)
        ids = log_ids if isinstance(log_ids, list) else [log_ids]

        for log_id in ids:
            entry: Log = self.logs[log_id]
            if entry.tier == LogTier.MUTABLE:
                raise PermissionError(
                    f"Log {log_id} has tier=MUTABLE. Use delete_log() instead."
                )
            if entry.tier == LogTier.IMMUTABLE:
                raise PermissionError(
                    f"Log {log_id} has tier=IMMUTABLE. Deletion is not permitted."
                )

            # Create IMMUTABLE evidence BEFORE deleting. If this emission fails,
            # deletion aborts (fail-closed, principle #1).
            deletion_record = ProtectedLogDeletion(
                deleted_log_id=str(entry.id),
                deleted_log_tier=entry.tier,
                break_glass_token_id=attestation.token_id,
                actor=attestation.actor,
                reason=reason,
                deleted_content_hash=_hash_content(entry.content),
            )
            deletion_evidence = Log.create(
                content=deletion_record,
                tier=LogTier.IMMUTABLE,
            )
            self.logs.include(deletion_evidence)

            self.logs.pop(entry.id)
            _log.warning(
                "PROTECTED log %s deleted by %s under break-glass %s. "
                "Evidence node %s created.",
                entry.id,
                attestation.actor,
                attestation.token_id,
                deletion_evidence.id,
            )

    def delete_protected_log(
        self,
        log_id: str | UUID,
        break_glass_token: str,
        reason: str,
    ) -> None:
        """Backward-compatible singular wrapper for one protected log."""
        self.delete_protected_logs(
            log_id,
            break_glass_token=break_glass_token,
            reason=reason,
        )

    def dump(
        self,
        clear: bool | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        """Dump logs to file(s), routing IMMUTABLE entries to a separate file.

        MUTABLE and PROTECTED entries go to the standard path.
        IMMUTABLE entries go to ``<stem>_immutable<ext>`` in append mode.
        The KHive backend opens that file append-only via DB triggers.

        IMMUTABLE entries are never cleared after dump.
        """
        if not self.logs:
            _log.debug("No logs to dump.")
            return

        fp = Path(persist_path) if persist_path else self._create_path()
        suffix = fp.suffix.lower()
        if suffix not in {".csv", ".json", ".jsonl"}:
            raise ValueError(f"Unsupported file extension: {suffix}")

        obj_key = "csv" if suffix == ".csv" else "json"
        immutable_fp = fp.with_stem(fp.stem + "_immutable")

        mutable_entries: Pile[Log] = Pile(
            collections=[e for e in self.logs if e.tier == LogTier.MUTABLE],
            item_type=Log,
            strict_type=True,
        )
        mutable_protected: Pile[Log] = Pile(
            collections=[e for e in self.logs if e.tier != LogTier.IMMUTABLE],
            item_type=Log,
            strict_type=True,
        )
        immutable_entries: Pile[Log] = Pile(
            collections=[e for e in self.logs if e.tier == LogTier.IMMUTABLE],
            item_type=Log,
            strict_type=True,
        )

        # Implementation delegates to existing Pile.dump() per-subset.
        # IMMUTABLE entries are always appended to the immutable partition.
        if mutable_protected:
            mutable_protected.dump(fp, obj_key)
        if immutable_entries:
            immutable_entries.dump(immutable_fp, obj_key, mode="a")

        do_clear = self._config.clear_after_dump if clear is None else clear
        if do_clear:
            # dump(clear=True) may rotate MUTABLE logs only. PROTECTED deletion
            # must pass through delete_protected_logs(); IMMUTABLE has no path.
            self.logs.exclude([entry.id for entry in mutable_entries])


def _hash_content(content: dict[str, Any]) -> str:
    payload = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return sha256(payload).hexdigest()
```

### 3. Tier assignment policy

The following table is the authoritative policy. Code emitting events in these categories
**must** set the corresponding tier. The `tier=` argument on `Log.create()` is the enforcement
point; there is no automatic classification.

| Event category | Tier | Examples |
|---|---|---|
| `log.debug.*`, raw stdin/stdout traces, perf timing | MUTABLE | `log.debug("token count=42")`, request latency |
| `branch.add_message()` calls | PROTECTED | Every message added to a Branch |
| Tool invocations (ActionRequest + ActionResponse) | PROTECTED | All `tool_call` events |
| Status transitions, errors | PROTECTED | `status=failed`, exception captures |
| Evidence emissions (ADR-0041) | IMMUTABLE | `EvidenceNode` creation events |
| Task certificate state changes (ADR-0042) | IMMUTABLE | `TaskCertificate` issue / revoke |
| Gate decisions (ADR-0044) | IMMUTABLE | HARD/SOFT gate ALLOW or DENY |
| Break-glass attestations (ADR-0045) | IMMUTABLE | Token issuance and consumption |
| Permit issuance/consumption (ADR-0046) | IMMUTABLE | JIT grant created or expired |
| Charter activations (ADR-0047) | IMMUTABLE | Charter bound to agent at init |
| Operation context captures (ADR-0050) | IMMUTABLE | Active-assertion snapshots |

### 4. The "audit of auditing" invariant

The core tamper-resistance property is:

> An agent that deletes a PROTECTED log cannot hide the act of deletion.

This is enforced structurally: `delete_protected_log()` creates the IMMUTABLE evidence node
*before* removing the entry. If the evidence emission raises (e.g., the backend is unavailable),
the deletion is aborted. This is the fail-closed default (cross-cutting principle #1): an error in
the audit trail blocks the operation that would create a gap in it.

IMMUTABLE entries have no deletion path — not even with break-glass. There is no
`delete_immutable_log()` method. Absence of API is the strongest enforcement: no administrative
shortcut, no emergency override, no code path to misuse.

### 5. Backend integration (KHive v1)

At the application layer (lionagi), `DataLogger` enforces tier semantics in Python. In the KHive
backend, the persistence layer adds complementary DB-level triggers following prior research:

- A `BEFORE DELETE` trigger on the IMMUTABLE log table raises an exception unconditionally.
- The `*_immutable.*` files written by `DataLogger.dump()` are opened in append-only mode by the
  KHive ingestion worker.
- PROTECTED log deletion events are written to the immutable table by the ingestion worker,
  providing a second enforcement layer independent of application code.

These backend constraints are KHive territory and are out of scope for this ADR. The application-
layer enforcement described in sections 2–4 is fully self-contained for lionagi v1.

## Consequences

**Positive**

- Tamper resistance at the application layer: a compromised agent or tool cannot erase gate
  decisions, evidence nodes, or charter activations via normal `DataLogger` operations.
- Audit-grade tier separation: operators can identify exactly which log entries constitute the
  governance record without scanning all entries.
- "Audit of auditing" closes the disable-logging attack vector: any PROTECTED log deletion
  produces its own permanent record.
- The three-tier model maps cleanly onto the existing `Log` + `DataLogger` design; no new
  architectural surface is required.

**Negative**

- IMMUTABLE entries accumulate indefinitely in the Pile and in the `*_immutable.*` files.
  Long-running sessions must tolerate unbounded growth in the immutable partition. Archival to
  cold storage is a KHive concern; lionagi has no automatic eviction.
- Every `Log.create()` call that should emit at PROTECTED or IMMUTABLE tier requires an explicit
  `tier=` argument. Callers that omit it silently default to MUTABLE — a regression risk during
  migration.
- `delete_protected_log()` depends on `lionagi.protocols.governance.break_glass` (ADR-0045),
  which is a sibling ADR not yet shipped. Until ADR-0045 is implemented, PROTECTED deletion raises
  an `ImportError` at runtime.
- Slight CPU cost: `__setattr__` on IMMUTABLE entries checks tier on every attribute write. In
  practice this is negligible; it only fires during Pydantic model initialization.

## Non-Goals

Explicitly out of scope:

- **Cryptographic signing of log entries.** Hash-chain integrity for evidence nodes is covered by
  ADR-0041. Log-level signing is a separate concern.
- **Log encryption at rest.** Encryption is a deployment and KHive backend concern, not a
  per-entry feature of `DataLogger`.
- **Distributed log replication.** Replication, WAL-based propagation, and multi-replica
  consistency are KHive infrastructure concerns.
- **Automatic tier classification.** Tier is always set explicitly at the call site. No heuristic,
  ML-based, or pattern-matching classifier determines tier post-hoc.
- **Multi-tenant log isolation.** Per-tenant log namespacing, row-level security, and tenant-scoped
  deletion policies are KHive territory. lionagi is a single-tenant library.
- **Retention schedules and archival.** ADR-0049 defines deletion semantics, not retention periods.
  Archival policy (e.g., 7-year PROTECTED retention per SOC2 CC6.2) is a deployment concern.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Single mutable log (current state) | Any agent with DataLogger access can erase its own activity record. No tamper resistance. |
| Append-only flag on DataLogger | Too coarse: prevents all deletion including legitimate MUTABLE rotation. Breaks long-running session memory management. |
| Separate file per tier only | Files can still be deleted by OS-level access. Doesn't prevent `DataLogger.clear()` from discarding in-memory IMMUTABLE entries before flush. |
| Application-flag immutability only (no API removal) | A `delete_immutable_log()` that raises on call is weaker than a method that does not exist. Callers can work around documented exceptions; they cannot call nonexistent methods. |
| Three-tier with automatic classification | Classification at emission time is correct; post-hoc classification has a window where entries are unclassified. Human + decorator assignment eliminates ambiguity. |

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — IMMUTABLE tier is the application-layer
  representation of evidence nodes; hash-chain integrity is defined there
- [ADR-0042](ADR-0042-task-certificate.md) — Task certificate state changes are IMMUTABLE tier
  events
- [ADR-0044](ADR-0044-tool-gates.md) — Gate decisions (HARD/SOFT/ADVISORY) are IMMUTABLE tier
  events
- [ADR-0045](ADR-0045-break-glass-protocol.md) — Required dependency for PROTECTED log deletion;
  provides `validate_break_glass_token()`
- [ADR-0046](ADR-0046-jit-tool-grant.md) — Permit issuance and consumption are IMMUTABLE tier
  events
- [ADR-0047](ADR-0047-agent-charter.md) — Charter activations are IMMUTABLE tier events
- [ADR-0050](ADR-0050-operation-context.md) — Operation context captures (active assertions) are
  IMMUTABLE tier events
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — `EvidenceRef` kinds map to IMMUTABLE log
  event categories
- prior governance research `01_design/034-audit-logging-governance/ADR-034-audit-logging-governance.md` — source
  pattern; three-tier hierarchy, DB trigger approach, audit-of-auditing invariant
