# ADR-0045: Break-Glass Protocol — DEGRADED-Defensibility Override

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0044](ADR-0044-tool-gates.md) (HARD gates being overridden), [ADR-0042](ADR-0042-task-certificate.md) (BREAK_GLASS certificate state), [ADR-0041](ADR-0041-immutable-evidence-nodes.md) (attestation as evidence node)
**Related**: [ADR-0050](ADR-0050-operation-context.md), [ADR-0047](ADR-0047-agent-charter.md), [ADR-0033](ADR-0033-unified-entity-state-model.md), [ADR-0043](ADR-0043-governed-tool-declaration.md)

---

## Context

lionagi today has no emergency override path. When an agent fails a HARD gate
(defined in [ADR-0044](ADR-0044-tool-gates.md)), it has two de-facto options:

1. **Fail silently** — the action is blocked, the operator sees an error, no
   record is produced that explains *why* the gate failed or that an override
   was even attempted.
2. **Proceed without record** — the caller catches the gate exception and
   re-invokes the tool with a flag that disables the check. This is entirely
   within reach today: `PermissionPolicy(mode="allowlist")` in
   `lionagi/agent/permissions.py` already accepts a caller-supplied config
   with zero audit hooks.

Neither outcome is acceptable for a governed agent platform. The first option
stalls legitimate emergencies; the second leaves a compliance gap with no
paper trail.

Cross-cutting principle #3 — *every constraint must be enforced, not just
documented* — applies to the override path itself. An override that produces
no record is not governance; it is a blank check disguised as a feature.

### The applicable design pattern

prior research frames this precisely: the problem is not whether an
emergency override is *possible*, but whether the resulting record
*acknowledges what happened*. Their solution distinguishes between
`defensibility: FULL` (normal path, all gates passed) and
`defensibility: DEGRADED` (break-glass path, gates failed, attestation
provided). The action is the same; the certificate is different. Both facts —
that the action happened, and that normal process was not followed — are
preserved in a single evidence node that cannot be amended after the fact
(ADR-0041).

### Why lionagi needs this

Consider: an autonomous coding agent holds a HARD gate that prevents
modification of files outside a declared project root. During a production
incident, an operator legitimately needs the agent to patch a shared config
file one directory above the root. The only current path is to re-instantiate
the agent with a looser `PermissionPolicy`. That change leaves no record, is
indistinguishable from a misconfiguration, and cannot be reviewed
post-incident. Break-glass replaces that informal workaround with a
first-class, auditable path that creates more friction than normal operation —
thereby preserving deterrence — while still letting the action succeed.

---

## Decision

We introduce `branch.break_glass(request)` as the single approved path for
overriding a HARD gate failure, producing a `TaskCertificate` with
`defensibility: DEGRADED` and a `BREAK_GLASS` evidence node that is
immutable, non-exportable by default, and triggers an immediate notification
event.

---

### 1. `BreakGlassReason` enum

```python
from enum import Enum

class BreakGlassReason(str, Enum):
    PRODUCTION_OUTAGE      = "production_outage"
    SECURITY_INCIDENT      = "security_incident"
    LEGAL_MANDATE          = "legal_mandate"
    SAFETY_THREAT          = "safety_threat"
    DATA_RECOVERY          = "data_recovery"
    AUTHORIZED_DRILL       = "authorized_drill"
```

The enum is closed. New reason codes require a code change and a reviewer
sign-off. This prevents operators from minting ad-hoc codes that dilute the
audit signal.

---

### 2. `BreakGlassRequest` dataclass

```python
from __future__ import annotations

import time
from typing import Literal

from pydantic import Field, field_validator

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.governance.evidence import EvidenceRef  # ADR-0033/0041


class BreakGlassRequest(Element):
    # Who is overriding and why
    reason_code: BreakGlassReason
    attestation_text: str                    # >= 50 chars, free-form
    actor_id: str                            # agent ln_id / user id / system id
    actor_role: Literal["agent", "user", "system"]

    # What is being overridden
    overridden_gates: tuple[str, ...] = Field(min_length=1)
    tool_name: str                           # the @governed_tool being unblocked

    # Scope
    expected_duration_minutes: int = Field(ge=1)
    supporting_evidence: Pile[EvidenceRef] = Field(
        default_factory=lambda: Pile(item_type={EvidenceRef}, strict_type=True)
    )

    # Filled at invocation time
    requested_at: float = Field(default_factory=time.time)

    @field_validator("attestation_text")
    @classmethod
    def _validate_attestation(cls, value: str) -> str:
        if len(value.strip()) < 50:
            raise ValueError(
                "attestation_text must be at least 50 characters. "
                "A checkbox is not attestation."
            )
        return value
```

The `__post_init__` validation enforces minimum attestation length at
construction time, not at invocation time. An invalid request cannot be
submitted — it raises before touching any gate.

---

### 3. `BreakGlassWindow` — the active override state

```python
import time
from typing import Literal

from pydantic import Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


class BreakGlassToolCall(Element):
    tool_name: str
    called_at: float = Field(default_factory=time.time)


class BreakGlassWindow(Element):
    request: BreakGlassRequest
    opened_at: float = Field(default_factory=time.time)
    expires_at: float | None = None
    closed_at: float | None = None
    tool_calls_during_window: Pile[BreakGlassToolCall] = Field(
        default_factory=lambda: Pile(
            item_type={BreakGlassToolCall},
            strict_type=True,
        )
    )
    closed_reason: Literal["expired", "explicit", "error"] | None = None

    def model_post_init(self, __context: object) -> None:
        if self.expires_at is None:
            self.expires_at = (
                self.opened_at + self.request.expected_duration_minutes * 60
            )

    @property
    def window_id(self) -> str:
        return str(self.id)

    @property
    def is_active(self) -> bool:
        return (
            self.closed_at is None
            and time.time() < self.expires_at
        )

    def record_tool_call(self, tool_name: str) -> None:
        """Every tool call during the window is tagged for the audit trail."""
        self.tool_calls_during_window.include(
            BreakGlassToolCall(tool_name=tool_name)
        )

    def close(self, reason: Literal["expired", "explicit", "error"]) -> None:
        self.closed_at = time.time()
        self.closed_reason = reason
```

---

### 4. `BreakGlassEvent` — the notification payload

```python
import time
from typing import Literal

from pydantic import Field

from lionagi.protocols.generic.element import Element


class BreakGlassEvent(Element):
    """Fired to operator/orchestrator immediately on window open and close."""
    event_type: Literal["opened", "closed"]
    window_id: str
    actor_id: str
    actor_role: str
    reason_code: str
    tool_name: str
    overridden_gates: tuple[str, ...]
    attestation_summary: str   # first 120 chars of attestation_text
    timestamp: float = Field(default_factory=time.time)
    branch_id: str
    session_id: str | None
    # On close only:
    tool_calls_count: int = 0
    certificate_id: str | None = None
    defensibility: Literal["DEGRADED"] = "DEGRADED"
```

Notifications are fired synchronously before the action executes (open event)
and after the window closes (close event). Both events are written to the
immutable evidence store (ADR-0041) before any downstream delivery. If
delivery fails, the evidence node is already committed.

---

### 5. `BreakGlassGuard` — gate-level non-overridability flag

Not all HARD gates are overridable. Gates that protect the agent system itself
from self-destruction are declared with `overridable=False`:

```python
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import Field

from lionagi.agent.config import AgentConfig
from lionagi.protocols.generic.element import Element


BreakGlassHook = Callable[[str, str, dict], Awaitable[dict | None]]


class GateDeclaration(Element):
    name: str
    tool_name: str = "*"
    level: Literal["HARD", "SOFT", "ADVISORY"]   # ADR-0044
    hook: BreakGlassHook = Field(exclude=True)
    overridable: bool = True
    override_actor_classes: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {"user", "system"}   # autonomous agents cannot self-override by default
        )
    )
    max_overrides_per_hour: int = 3

    def register(self, config: AgentConfig) -> None:
        config.hook_handlers.setdefault(
            f"security_pre:{self.tool_name}",
            [],
        ).insert(0, self.hook)
```

When `overridable=False`, `break_glass()` raises `NonOverridableGateError`
regardless of attestation content or actor role. The list of non-overridable
gates is enumerated in the agent charter (ADR-0047) and cannot be modified at
runtime.

Gates that are overridable by `user` or `system` but NOT by `agent` enforce
the rule that autonomous agents cannot unilaterally override their own
constraints. An agent that detects a gate failure must surface the failure to
its orchestrator, not invoke break-glass on its own authority.

---

### 6. `branch.break_glass()` — the API surface

```python
from __future__ import annotations

from collections.abc import Callable

from lionagi.protocols.governance.break_glass import (
    BreakGlassEvent,
    BreakGlassRequest,
    BreakGlassWindow,
    NonOverridableGateError,
    RateLimitExceededError,
)
from lionagi.protocols.generic.log import Log
from lionagi.protocols.generic.pile import Pile


class Branch:
    # ... existing Branch surface ...

    async def break_glass(
        self,
        request: BreakGlassRequest,
        *,
        notify: Callable[[BreakGlassEvent], None] | None = None,
    ) -> BreakGlassWindow:
        """
        Override one or more HARD gate failures with a typed attestation.

        Returns an active BreakGlassWindow. The caller is responsible for
        explicitly closing the window via `await window.aclose()` when the
        emergency action completes. If not closed, the window expires
        automatically at `window.expires_at`.

        Raises:
            NonOverridableGateError  — if any gate in request.overridden_gates
                                       has overridable=False.
            RateLimitExceededError   — if actor_id has exceeded
                                       max_overrides_per_hour for any gate.
            ValueError               — if request validation fails (re-raised
                                       from BreakGlassRequest validation).
        """
        if request.tool_name not in self.acts.registry:
            raise ValueError(f"Tool {request.tool_name!r} is not registered.")

        self._validate_actor_class(request)       # enforce override_actor_classes
        self._check_non_overridable(request)      # raise NonOverridableGateError early
        self._check_rate_limit(request)           # raise RateLimitExceededError early

        window = BreakGlassWindow(request=request)

        open_event = BreakGlassEvent(
            event_type="opened",
            window_id=window.window_id,
            actor_id=request.actor_id,
            actor_role=request.actor_role,
            reason_code=request.reason_code,
            tool_name=request.tool_name,
            overridden_gates=request.overridden_gates,
            attestation_summary=request.attestation_text[:120],
            branch_id=str(self.id),
            session_id=str(self.user) if self.user else None,
        )

        # Fail closed: write the Element snapshot through Branch's DataLogger
        # before notifying or enabling the existing hook-based tool path.
        await self._log_manager.alog(Log.create(open_event))
        if notify is not None:
            notify(open_event)

        windows = self.metadata.setdefault(
            "active_break_glass_windows",
            Pile(item_type={BreakGlassWindow}, strict_type=True),
        )
        windows.include(window)
        return window
```

The method is `async` because committing the evidence node and firing the
notification are I/O operations. The HARD gate check that originally triggered
the failure is NOT re-evaluated inside `break_glass()` — that check already
ran; the attestation is the response to it, not a second pass.

---

### 7. Window lifecycle and certificate minting

```text
Agent calls tool
      |
  HARD gate fails
      |
  Caller invokes branch.break_glass(request)
      |
  [Validation] ──── fail ──── raise (no window created)
      |
  Evidence node committed (immutable, exportable=False)
      |
  BreakGlassEvent("opened") fired
      |
  BreakGlassWindow returned (is_active=True)
      |
  Action executes; every tool call recorded in window.tool_calls_during_window
      |
  Window closes (explicit close or expiry)
      |
  TaskCertificate minted:
      defensibility = "DEGRADED"
      state         = "BREAK_GLASS"
      evidence_refs = [break_glass_node, ...supporting_evidence]
      |
  BreakGlassEvent("closed") fired with certificate_id
```

The certificate (ADR-0042) is minted at window close, not at window open. If
the window expires without an explicit close, the expiry trigger mints the
certificate automatically. There is no path from open to close that does not
produce a certificate.

---

### 8. Non-exportability

Break-glass evidence nodes and the certificates that reference them carry
`exportable: False` by default. This flag is enforced by the evidence store
(ADR-0041) at read time, not just at write time.

Programmatic export requires:

1. An elevated permission grant (`EXPORT_BREAK_GLASS`) on the calling actor.
2. An explicit export request that itself produces an audit evidence node,
   recording who exported what and when.

This is not obscurity — the evidence node exists and is queryable by authorized
operators. Non-exportability means it cannot leave the platform boundary in a
bulk data export without review.

---

### 9. Rate limiting and cooldown

```python
import time

from pydantic import Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.governance.break_glass import RateLimitExceededError


class BreakGlassRateLimitInvocation(Element):
    actor_id: str
    gate_name: str
    invoked_at: float = Field(default_factory=time.time)


class BreakGlassRateLimit(Element):
    actor_id: str
    gate_name: str
    max_per_hour: int = 3
    window_seconds: int = 3600
    # runtime-tracked
    invocations: Pile[BreakGlassRateLimitInvocation] = Field(
        default_factory=lambda: Pile(
            item_type={BreakGlassRateLimitInvocation},
            strict_type=True,
        )
    )

    def check(self) -> None:
        now = time.time()
        for invocation in list(self.invocations):
            if now - invocation.invoked_at >= self.window_seconds:
                self.invocations.exclude(invocation)
        if len(self.invocations) >= self.max_per_hour:
            raise RateLimitExceededError(
                f"Actor {self.actor_id!r} has used break-glass on gate "
                f"{self.gate_name!r} {self.max_per_hour} times in the last hour. "
                "Contact your operator to reset."
            )
        self.invocations.include(
            BreakGlassRateLimitInvocation(
                actor_id=self.actor_id,
                gate_name=self.gate_name,
            )
        )
```

Rate limits are per-actor, per-gate, per-hour. Exceeding the limit does not
silently allow the action — it raises `RateLimitExceededError`. The limit
itself is configurable per gate declaration but defaults to 3/hour, which is
generous for genuine emergencies and punishing for misuse.

---

## Consequences

**Positive**

- Every HARD gate override produces a complete, immutable audit trail with a
  dated attestation, actor identity, overridden gate names, and a certificate
  marked DEGRADED. Post-incident review has all the facts.
- The DEGRADED defensibility state gives operators, auditors, and (where
  applicable) legal counsel an accurate signal about what process was followed.
  "We did it with DEGRADED defensibility" is a defensible position; "we did it
  with no record" is not.
- Non-overridable gates (`overridable=False`) eliminate the risk that break-glass
  becomes a universal escape hatch. The most critical constraints are
  permanently out of scope.
- Friction by design: the minimum 50-char attestation, the actor-class
  restriction on autonomous agents, and the rate limit make break-glass slower
  than normal operation. Legitimate emergencies tolerate this friction; routine
  misuse does not survive the rate limit.
- The pattern composes with Operation Context (ADR-0050): every tool call during
  a break-glass window can carry `context_id=window.window_id` in its evidence
  node, creating a complete timeline of what happened under the override.

**Negative**

- Surface area: `BreakGlassRequest`, `BreakGlassWindow`, `BreakGlassEvent`, and
  the `break_glass()` method are new API that must be maintained.
- Operator burden: the notification mechanism requires a subscriber. If no
  subscriber is configured, notifications queue silently. The default behavior
  must be to log to the immutable evidence store regardless.
- Genuinely time-critical emergencies may find the 50-char attestation a
  bottleneck. The minimum is low enough (roughly two sentences) that this
  concern is largely theoretical, but it is real.
- In library mode, the rate-limit and non-exportability semantics are enforced
  in-process and can be bypassed by a caller with full Python access. Full
  enforcement requires a governed evidence layer with durable storage.

---

## Non-Goals

Explicitly out of scope:

- **Automatic break-glass approval**: no system or agent can approve its own
  break-glass request. Human attestation is a requirement, not a suggestion.
  Confidence scores, risk scores, and audit history do not substitute for a
  typed justification from an accountable actor.
- **Break-glass without a certificate**: there is no `silent_override()` or
  `emergency_skip()`. Every invocation of `break_glass()` that does not raise
  produces a certificate. The two are inseparable.
- **Window expiry without certificate mint**: a window that expires without an
  explicit close still mints a certificate. There is no path where a window
  opens and no certificate is ever produced.
- **Cascading break-glass**: a break-glass window does not implicitly expand to
  cover subsequent gate failures. Each failing gate in a new action requires
  its own `overridden_gates` declaration. Open-ended "override everything" is
  not supported.
- **Policy customization**: which reason codes are available in a given deployment
  context is a deployment-layer concern and is not defined here.
- **Retroactive reclassification**: a DEGRADED certificate cannot be upgraded
  to FULL defensibility after the fact, even if subsequent review concludes the
  action was correct. The state at time of action is preserved permanently.

---

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Silent override with post-hoc logging | No record at the time of decision. If the system crashes after the action but before the log write, there is no evidence the override happened. Violates ADR-0041 (evidence committed before action). |
| Reject without any override path | Legitimate production emergencies exist. A governance system that cannot accommodate genuine emergencies will be worked around informally, producing worse audit trails than break-glass. |
| Confidence-based automatic override | Defeats the fail-closed principle (cross-cutting principle #1). If a gate can be bypassed by meeting a confidence threshold, the gate is not HARD — it is a SOFT gate with a confidence flavor. Rename it honestly or keep it HARD. |
| Simple `emergency: bool` flag on existing tool calls | No defensibility acknowledgment, no rate limit, no actor-class restriction, no evidence node, no certificate. This is the "add a comment field to a checkbox" pattern, not governance. |
| Checkbox-based reason selection | Predefined checkboxes have no accountability signal. A 50-char attestation requires the actor to formulate a sentence, which creates cognitive ownership of the decision. prior research tested this distinction directly. |

---

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — attestation is an immutable evidence node; committed before action executes
- [ADR-0042](ADR-0042-task-certificate.md) — defines `TaskCertificate`, `defensibility: DEGRADED`, and `BREAK_GLASS` state
- [ADR-0043](ADR-0043-governed-tool-declaration.md) — `@governed_tool` declares which tools carry gates
- [ADR-0044](ADR-0044-tool-gates.md) — HARD/SOFT/ADVISORY gate levels; HARD gates are what break-glass overrides
- [ADR-0047](ADR-0047-agent-charter.md) — agent charters enumerate which gates are `overridable=False`
- [ADR-0050](ADR-0050-operation-context.md) — operation context carried on every tool call during a break-glass window
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — `EvidenceRef` type used in `BreakGlassRequest.supporting_evidence`
- prior governance research `01_design/016-break-glass/ADR-016-break-glass.md` — source pattern (DEGRADED defensibility, typed attestation, auto-notification, non-exportable default)
