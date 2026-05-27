# ADR-0046: JIT Tool Grant — No Standing Capability for High-Risk Tools

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0043](ADR-0043-governed-tool-declaration.md), [ADR-0044](ADR-0044-tool-gates.md)
**Related**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md), [ADR-0045](ADR-0045-break-glass-protocol.md), [ADR-0047](ADR-0047-agent-charter.md), [ADR-0051](ADR-0051-tool-registry-allowlists.md)

---

## Context

In lionagi today, a tool registered on a `Branch` is always callable. The moment
`branch.register_tools([delete_resource, write_production_db, call_payment_api])` executes, the
agent has standing capability to invoke any of those tools at any point in any future turn — with
no fresh authorization, no time limit, no scope binding. Nothing prevents the agent from calling
`delete_resource(id="all")` three sessions after the operator intended a one-off cleanup. The
capability persists until the Branch is torn down.

For low-risk tools (read operations, local file reads, formatting utilities), standing capability is
acceptable and desirable — requiring authorization on every call would make agentic workflows
unusable. For high-risk tools — production writes, financial transactions, external API calls with
side effects, irreversible destructive operations — standing capability is the attack surface. An
agent that is compromised, confused, or simply operating outside its intended scope can cause
real harm precisely because the capability is always present.

Cross-cutting principle #1 (fail-closed is universal default) requires that ambiguity about
authorization leads to denial, not execution. Cross-cutting principle #3 (every constraint must
be enforced, not just documented) means a `requires_jit=True` annotation on a tool has no value
unless the runtime actually blocks execution when no valid grant exists. This ADR introduces the
`JITGrant` and `PermitToken` constructs that make both principles operational for high-risk tools.

### The applicable prior governance research insight

prior research addresses standing capability in human-facing sensitive operations (terminations,
compensation changes) using a two-layer model: a `PermitToken` that binds one certificate to one
execution (replay prevention), and a JIT role that removes the standing capability from the actor
entirely so the action is invisible in the UI until the grant is active. The core insight
translates directly to agent governance: the primary mechanism is the permit (transaction binding,
single-use); the secondary mechanism is JIT (no standing power). Both layers must hold. Disabling
either degrades security — permit-only allows replay within the window; JIT-only allows replay
because there is no per-transaction binding.

### Why lionagi needs this

Consider a `Branch` configured with `write_billing_record` and `archive_customer`. An operator
launches the agent to process a single end-of-month billing run. The agent completes the task.
The Branch is reused (as is common in FlowAgent pipelines — ADR-0047 documents that the same
`branch=` reuse accumulates state). Two turns later, in a different context, the agent encounters
an instruction it misinterprets as a correction request and calls `archive_customer(id="batch_*")`.
The capability was standing. No gate fired. Without JIT grants, there is no mechanism to ensure
that `write_billing_record` and `archive_customer` are only callable during a specific authorized
window for a specific operation, not generally at any time the agent is alive.

---

## Decision

Introduce `PermitToken` and `JITGrant` as the authorization substrate for high-risk tools.
Tools declared with `requires_jit=True` via `@governed_tool` (ADR-0043) cannot execute unless
a valid, unexpired `PermitToken` exists for the `(agent_id, tool_id)` pair at call time; the
token is consumed atomically on first redemption; and a `JITGrant` record is held for the
duration of that execution window.

---

### 1. Schemas

The grant types are frozen `Element` records. State changes produce new instances; no mutation
in place. The Branch-owned grant manager keeps active grant state in typed `Pile` collections.

```python
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import ConfigDict, Field

from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger
from lionagi.protocols.generic.pile import Pile


FROZEN_RECORD = ConfigDict(
    arbitrary_types_allowed=True,
    use_enum_values=True,
    populate_by_name=True,
    extra="forbid",
    frozen=True,
)


class GrantScope(Element):
    """Scope that binds a grant to one agent, one tool, and optional args."""

    model_config = FROZEN_RECORD

    agent_id: str
    tool_id: str
    argument_constraints: dict[str, Any] = Field(default_factory=dict)

    def matches(
        self,
        *,
        agent_id: str,
        tool_id: str,
        arguments: dict[str, Any],
    ) -> bool:
        return (
            self.agent_id == agent_id
            and self.tool_id == tool_id
            and not self.constraint_violations(arguments)
        )

    def constraint_violations(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            key: {"expected": expected, "actual": arguments.get(key)}
            for key, expected in self.argument_constraints.items()
            if arguments.get(key) != expected
        }


class PermitRequest(Element):
    """Agent-facing request emitted by branch.request_permit(...)."""

    model_config = FROZEN_RECORD

    scope: GrantScope
    requested_by: str
    reason: str = ""
    action_request_id: str | None = None
    deadline_at: float | None = None


class ApprovalEvidence(Element):
    """Evidence record for approval, denial, timeout, consumption, or revocation."""

    model_config = FROZEN_RECORD

    request_id: str | None = None
    principal_id: str
    decision: Literal["approved", "denied", "timeout", "consumed", "revoked"]
    reason: str = ""
    source_log_ids: tuple[str, ...] = ()


class PermitToken(Element):
    """Single-use authorization token bound to one (agent, tool) execution."""

    model_config = FROZEN_RECORD

    token_id: str                       # idempotency key; Element.id is canonical UUID
    scope: GrantScope
    issued_by: str                      # human user-id, orchestrator-id, or "system"
    issued_at: float                    # unix timestamp
    expires_at: float                   # issued_at + window (e.g., 300 seconds)
    approval_evidence_id: str
    consumed: bool = False
    consumed_at: float | None = None
    consumed_by_action_request_id: str | None = None

    def is_valid(self, at: float | None = None) -> bool:
        """True iff unexpired and not yet consumed."""
        t = at if at is not None else time.time()
        return (not self.consumed) and (t < self.expires_at)

    def consume(self, *, action_request_id: str) -> "PermitToken":
        """Return a new instance with consumed=True. Call site must persist."""
        if self.consumed:
            raise ValueError(f"PermitToken {self.token_id} already consumed")
        return self.model_copy(
            update={
                "consumed": True,
                "consumed_at": time.time(),
                "consumed_by_action_request_id": action_request_id,
            }
        )


class JITGrant(Element):
    """Time-bound capability record for a (agent, tool) pair.

    Separate from PermitToken: the grant records that capability was lawfully
    extended; the permit is the per-call proof of authorization. Both must be
    present and valid for execution to proceed.
    """

    model_config = FROZEN_RECORD

    grant_id: str
    scope: GrantScope
    valid_from: float
    valid_until: float                  # hard expiry for capability window
    issued_by: str
    revoke_after: float                 # >= valid_until + 300 s (delayed revocation)
    permit_token_id: str                # back-reference to the PermitToken
    approval_evidence_id: str
    revoked_at: float | None = None
    revocation_evidence_id: str | None = None

    def is_active(self, at: float | None = None) -> bool:
        t = at if at is not None else time.time()
        return self.revoked_at is None and self.valid_from <= t <= self.valid_until

    def revoke(self, *, evidence_id: str) -> "JITGrant":
        return self.model_copy(
            update={
                "revoked_at": time.time(),
                "revocation_evidence_id": evidence_id,
            }
        )


class JITGrantManager(Element):
    """Branch-owned grant state; mounted beside ActionManager and DataLogger."""

    agent_id: str
    actions: ActionManager | None = Field(default=None, exclude=True)
    logger: DataLogger | None = Field(default=None, exclude=True)
    permit_requests: Pile[PermitRequest] = Field(
        default_factory=lambda: Pile(item_type=PermitRequest, strict_type=True)
    )
    approval_evidence: Pile[ApprovalEvidence] = Field(
        default_factory=lambda: Pile(item_type=ApprovalEvidence, strict_type=True)
    )
    permit_tokens: Pile[PermitToken] = Field(
        default_factory=lambda: Pile(item_type=PermitToken, strict_type=True)
    )
    grants: Pile[JITGrant] = Field(
        default_factory=lambda: Pile(item_type=JITGrant, strict_type=True)
    )

    def record_evidence(self, evidence: ApprovalEvidence) -> None:
        self.approval_evidence.include(evidence)
        if self.logger:
            self.logger.log(evidence)

    def find_valid_token(
        self,
        *,
        agent_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        at: float | None = None,
    ) -> PermitToken | None:
        t = at if at is not None else time.time()
        for token in self.permit_tokens:
            if not token.is_valid(t):
                continue
            if not token.scope.matches(
                agent_id=agent_id, tool_id=tool_id, arguments=arguments
            ):
                continue
            for grant in self.grants:
                if (
                    grant.permit_token_id == token.token_id
                    and grant.scope == token.scope
                    and grant.is_active(t)
                ):
                    return token
        return None

    def consume_token(
        self,
        *,
        token: PermitToken,
        action_request_id: str,
    ) -> PermitToken:
        consumed = token.consume(action_request_id=action_request_id)
        self.permit_tokens.update(consumed)
        self.record_evidence(
            ApprovalEvidence(
                principal_id="gate_jit_required",
                decision="consumed",
                reason=f"PermitToken {token.token_id} consumed for execution.",
                request_id=action_request_id,
            )
        )
        return consumed
```

---

### 2. Standing vs. JIT capability

A tool's capability mode is set at declaration time via `@governed_tool` (ADR-0043):

```python
from lionagi.agent.governance.governed_tool import governed_tool
from lionagi.protocols.action.manager import ActionManager

# Default: STANDING — callable at any time, no JIT required.
@governed_tool
async def read_document(path: str) -> str: ...

# JIT required: gate_jit_required fires before every call.
@governed_tool(requires_jit=True, safety_class="privileged")
async def delete_resource(resource_id: str) -> dict: ...

# safety_class="privileged" implies requires_jit=True even if omitted.
@governed_tool(safety_class="privileged")
async def write_production_record(payload: dict) -> dict: ...

actions = ActionManager()
actions.register_tools(
    [read_document, delete_resource, write_production_record],
    update=True,
)
```

The `safety_class` field interacts with the Tool Registry Allowlist (ADR-0051): tools registered
as `safety_class="privileged"` appear in the privileged tier of the allowlist and JIT is enforced
at the registry level, not just on the individual tool decorator. An agent cannot register a
privileged tool and bypass JIT by omitting `requires_jit=True`.

---

### 3. Execution flow

The `gate_jit_required` gate (registered as a HARD gate per ADR-0044) fires before any
`requires_jit=True` tool call. The gate is registered through the existing `AgentConfig.pre(...)`
hook path and closes over the current Branch's `JITGrantManager`.

```python
from typing import Any

from lionagi.agent.config import AgentConfig
from lionagi.session.branch import Branch


def install_jit_grant_hook(branch: Branch, config: AgentConfig) -> JITGrantManager:
    """Mount Branch-scoped grants and register the JIT gate through AgentConfig."""
    grants = JITGrantManager(
        agent_id=str(branch.id),
        actions=branch.acts,             # existing ActionManager
        logger=branch._log_manager,      # existing DataLogger
    )
    branch.metadata.setdefault("governance", {})["jit_grants"] = str(grants.id)
    config.pre("*", gate_jit_required(grants))
    return grants


def gate_jit_required(grants: JITGrantManager):
    """Existing pre-hook signature from lionagi.agent.hooks."""

    async def _gate(
        tool_name: str,
        action: str,
        args: dict[str, Any],
    ) -> dict | None:
        tool_id = str(args.get("function") or args.get("tool_id") or action or tool_name)
        tool = grants.actions.registry.get(tool_id) if grants.actions else None
        governance = getattr(tool, "governance", None) or getattr(
            tool, "governance_meta", None
        )
        requires_jit = bool(getattr(governance, "requires_jit", False))
        safety_class = getattr(governance, "safety_class", "")
        if not (requires_jit or safety_class == "privileged"):
            return None

        agent_id = str(args.get("agent_id") or grants.agent_id)
        token = grants.find_valid_token(
            agent_id=agent_id,
            tool_id=tool_id,
            arguments=args,
        )
        if token is None:
            reason = (
                f"No valid JIT permit for tool '{tool_id}' / agent '{agent_id}'. "
                "Call branch.request_permit(tool_id, args) to obtain authorization."
            )
            grants.record_evidence(
                ApprovalEvidence(
                    principal_id="gate_jit_required",
                    decision="denied",
                    reason=reason,
                )
            )
            raise PermissionError(reason)

        violations = token.scope.constraint_violations(args)
        if violations:
            reason = f"Permit constraint violations: {violations}"
            grants.record_evidence(
                ApprovalEvidence(
                    principal_id="gate_jit_required",
                    decision="denied",
                    reason=reason,
                )
            )
            raise PermissionError(reason)

        grants.consume_token(
            token=token,
            action_request_id=str(args.get("action_request_id", "")),
        )
        return None

    return _gate
```

Sequence: agent generates `ActionRequest` → gate fires → no valid token → HARD deny with
`request_permit` path → agent calls `branch.request_permit(tool_id, args)` → authorizing party
issues `PermitToken` + `JITGrant` → agent retries → gate validates and consumes token atomically
→ tool executes; `ActionResponse` recorded as evidence (ADR-0041) → grant held until
`revoke_after`; any second call without a new token fails (fail-closed, principle #1).

---

### 4. Permit request surface

`branch.request_permit(tool_id, args, reason="")` is the agent-facing entry point. It emits a
`PermitRequest` event and suspends the current turn (does not consume context window tokens)
until the authorizing party responds. It raises `PermitDenied` on rejection and `PermitTimeout`
if no decision arrives within the configured deadline.

In autonomous pipelines, the orchestrator acts as the authorizing party. A `Branch` configured
as an orchestrator (ADR-0047) can issue permits for sub-agents it governs, provided the
orchestrator itself holds a grant from a human principal at the session level. This is not
self-approval — ADR-0048 (Segregation of Duties) requires the authorizing identity to be
distinct from the executing agent.

---

### 5. Delayed revocation rationale

`JITGrant.revoke_after` is set to `valid_until + 300 seconds` by default. A grant revoked at
exactly `valid_until` may leave an in-flight tool call (already dispatched, awaiting network I/O)
in an undefined authorization state. The 5-minute grace window lets legitimately dispatched calls
complete or receive a clean denial from the downstream system rather than a mid-call revocation.

The grace window does NOT extend the permit. `PermitToken` is consumed on first redemption
regardless of whether `JITGrant` is still in grace — replay prevention is enforced by the token,
not the window. An agent that consumed its token and attempts a second call during the grace
period fails at the gate (no valid token found).

---

### 6. Audit integration

Every state transition in the JIT lifecycle produces an evidence node per ADR-0041:

| Event | Evidence kind | Required fields |
|-------|---------------|-----------------|
| `PermitToken` issued | `tool_result` | `token_id`, `issued_by`, `expires_at`, `tool_id`, `agent_id` |
| `PermitToken` consumed | `tool_result` | `token_id`, `consumed_at`, `action_request_id` |
| `PermitToken` expired (unused) | `tool_result` | `token_id`, `expires_at` |
| `JITGrant` issued | `tool_result` | `grant_id`, `issued_by`, `valid_until`, `revoke_after` |
| `JITGrant` revoked | `tool_result` | `grant_id`, `revoked_at` |
| Gate denial | `tool_result` | `tool_id`, `agent_id`, `reason`, `gate_id=gate_jit_required` |

Evidence nodes are hash-chained (ADR-0041) so that the sequence — permit issued, consumed,
grant revoked — cannot be silently rewritten after the fact. This provides the audit trail
required for post-incident reconstruction: given any tool call, the auditor can traverse
`action_request_id → permit_token_id → grant_id → issued_by` without gaps.

---

## Consequences

**Positive**

- No standing capability for tools declared `requires_jit=True` or `safety_class="privileged"`.
  A compromised or out-of-scope agent cannot invoke high-risk tools without a fresh authorization
  decision from an external party.
- Replay prevention is structural, not convention-based. A `PermitToken` consumed once cannot
  authorize a second call regardless of whether the grant window is still open.
- Constraint binding narrows the blast radius. A permit issued with `{"resource_id": "r-123"}`
  blocks the agent from calling the same tool with `resource_id="r-*"` even if no human is
  watching.
- Audit trail is machine-traversable from tool call back to human decision (ADR-0041 evidence
  chain, ADR-0042 Task Certificate).
- Break-glass compatibility: ADR-0045 provides the emergency path when the grant issuance
  system is unavailable. Break-glass is not a JIT bypass — it is a separate degraded-mode
  protocol that produces its own evidence under heightened scrutiny.

**Negative**

- Agentic workflows that previously ran autonomously on high-risk tools now require an external
  authorization step. Pipelines must be redesigned to either obtain permits ahead of time (batch
  pre-authorization) or tolerate suspension while awaiting approval.
- Human-in-the-loop latency is now on the critical path for any `requires_jit=True` tool. The
  operator interface must surface permit requests clearly; a buried notification degrades the
  flow without improving security.
- Orchestrators acting as authorizing parties create a trust chain that must be audited. An
  orchestrator that auto-approves every sub-agent request without human oversight negates the
  security benefit. ADR-0047 (Agent Charter) and ADR-0048 (Segregation of Duties) constrain
  what an orchestrator may self-approve.
- `JITGrantManager` introduces shared mutable state across a Branch's execution that must be
  thread-safe and persisted to survive process restarts. Implementations that hold grants only
  in memory lose the revocation record on crash.

---

## Non-Goals

Explicitly out of scope:

- **Federated permit issuance**: permits are scoped to a single lionagi process/session
  boundary.
- **Automatic re-issuance on expiry**: A `PermitToken` that expires is not automatically renewed.
  Re-issuance requires a new authorization decision. Automatic renewal would convert a time-bounded
  grant into standing capability under a different name.
- **Grant transferability between agents**: A `PermitToken` is bound to `GrantScope.agent_id`. It
  cannot be passed from one Branch to another. An agent cannot delegate its own permit.
- **Permit-less execution paths for declared high-risk tools**: There is no flag, environment
  variable, or runtime mode that makes `requires_jit=True` tools executable without a valid
  token. Library mode (`LIONAGI_GOVERNED=false`) suppresses governance at the framework level,
  but does not affect tools that explicitly declare `requires_jit=True` in their own decorator.
- **UI permit-approval interface**: The UI surface for displaying and approving permit requests
  is out of scope for this ADR. The protocol (`PermitRequest` event, `request_permit` API) is
  defined here; the UI implementation is a separate concern.
- **Revocation of already-consumed tokens**: A consumed token is a historical record, not an
  active authorization. Revoking it has no security effect and is not supported.

---

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Standing capability only (current behavior) | No authorization boundary for high-risk tools. A long-lived agent with `delete_resource` registered can call it at any turn in any context. Unacceptable for privileged operations. |
| Revoke standing on idle timeout | Too coarse. Idle-based revocation does not bind capability to a specific authorized operation. An agent that just processed an unrelated request would have its capability restored on the next active turn. |
| Per-call human confirmation | Blocks agentic execution — every call suspends until a human responds. For multi-step tool sequences this creates an unworkable approval queue. JIT grants allow pre-authorization of a bounded window; per-call confirmation eliminates any window. |
| JIT role only (no permit token) | Removes standing capability but allows replay: any call during the grant window is authorized, not just the specific transaction the human approved. prior research explicitly rejected this. Replay within the grant window is a real attack vector. |
| Permit token only (no JIT grant record) | Prevents replay but provides no capability-window audit record. The grant record is what allows auditors to trace "who extended capability to this agent and for how long" independently of which specific calls were made. |

---

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — hash-chained evidence nodes that anchor every permit lifecycle event
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate: signed proof of process adherence that JIT grants link into
- [ADR-0043](ADR-0043-governed-tool-declaration.md) — `@governed_tool` declaration surface; `requires_jit=True` and `safety_class` fields defined there
- [ADR-0044](ADR-0044-tool-gates.md) — Tool Gates (HARD/SOFT/ADVISORY); `gate_jit_required` is a HARD gate registered per this ADR
- [ADR-0045](ADR-0045-break-glass-protocol.md) — emergency path when grant issuance is unavailable; not a JIT bypass
- [ADR-0047](ADR-0047-agent-charter.md) — Agent Charter; constrains which agents may act as authorizing parties
- [ADR-0048](ADR-0048-agent-segregation-of-duties.md) — SoD: an agent cannot approve its own JIT grant
- [ADR-0051](ADR-0051-tool-registry-allowlists.md) — privileged tier of the tool registry enforces JIT at registration time
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — `EvidenceRef` kinds used in audit table above
- prior governance research `01_design/015-jit-role/ADR-015-jit-role.md` — source pattern (defense-in-depth with permit + JIT roles)
