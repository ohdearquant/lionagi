# ADR-0044: Tool Gates — Three-Tier Binary Enforcement

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0043](ADR-0043-governed-tool-declaration.md), [ADR-0041](ADR-0041-immutable-evidence-nodes.md)
**Related**: [ADR-0045](ADR-0045-break-glass-protocol.md), [ADR-0050](ADR-0050-operation-context.md), [ADR-0046](ADR-0046-jit-tool-grant.md), [ADR-0033](ADR-0033-unified-entity-state-model.md)

---

## Context

lionagi today has three independent mechanisms that collectively serve as access control over
tool calls: `PermissionPolicy` (allowlist/denylist/escalate mode in `lionagi/agent/permissions.py`),
ad-hoc pre-hooks registered via `AgentConfig.pre()`, and two named hooks (`guard_destructive`,
`guard_paths` in `lionagi/agent/hooks.py`). These mechanisms differ in failure semantics,
composition, and auditability in ways that create gaps.

`PermissionPolicy` operates on glob patterns and expresses three behaviors — allow, deny, escalate
— but carries no evidence and does not distinguish between a hard safety requirement and a soft
business rule. `guard_destructive` raises `PermissionError` unconditionally for any destructive
command, with no path to justification or escalation. `guard_paths` likewise raises with no
override mechanism. `log_tool_use` produces a log entry but contributes no evidence to the
operation record. The result is that a framework consumer must reach into three different systems
to answer the question "what constraints are active on this tool call, and can they be overridden?"

Two of the three cross-cutting principles this governance slate enforces apply directly here.
Principle 1 (fail-closed is the universal default) has no systematic expression today: some hooks
raise on failure, some return `None`, and if an evaluator crashes the caller receives an unhandled
exception rather than a clean deny. Principle 3 (every constraint must be enforced, not just
documented) is violated when a `PermissionPolicy` in `mode="allow_all"` is in force and all hooks
are simply not registered — there is no mechanism to guarantee a gate runs regardless of session
configuration.

### The applicable prior governance research insight

prior research establishes that gates return `passed: bool` — no intermediate states, no
confidence scores. The reasoning is precise: authorization is a question of fact, not of
probability. "Did the user consent to this write?" has a yes or no answer. "Was this action
covered by the granted capability?" has a yes or no answer. Confidence scores introduce threshold
debates, legal ambiguity, and—critically—they shift the locus of the authorization decision from
the gate to whoever chose the threshold. Binary semantics eliminate that ambiguity entirely.
prior governance research also introduces the three-tier model (HARD_MANDATORY, SOFT_MANDATORY, ADVISORY) directly
inspired by HashiCorp Sentinel, distinguishing irrevocable requirements from overridable business
rules from informational warnings.

### Why lionagi needs this

A coding agent with a file-editor tool and no explicit gate registration can overwrite files
outside the project workspace if `PermissionPolicy` is in `mode="allow_all"` (the default for
orchestrators). Nothing in the current system guarantees that `guard_paths` runs. When an agent
in a governed context (KHive tenant, Lion Studio session with audit logging) executes a write
outside bounds, there is no evidence artifact recording that the write was ungated — the
operation silently succeeds and appears identical to an explicitly approved one. The gate tier
structure ensures that HARD gates cannot be absent from governed tools (enforced by
[ADR-0043](ADR-0043-governed-tool-declaration.md)), and that every gate evaluation — whether
passed or failed — produces a `GateResult` that becomes evidence in the operation record
(enforced by [ADR-0050](ADR-0050-operation-context.md)).

---

## Decision

We introduce `ToolGate`, a registered binary predicate with one of three enforcement tiers
(HARD_MANDATORY, SOFT_MANDATORY, ADVISORY), where every evaluation produces a `GateResult` that
is immutable evidence, and where any evaluator exception resolves to `passed=False` rather than
propagating.

---

### 1. Core Types

```python
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

if TYPE_CHECKING:
    from lionagi.protocols.messages.action_request import ActionRequest
    from lionagi.session.branch import Branch
    from lionagi.protocols.governance.evidence import EvidenceRef


class GateEnforcement(Enum):
    """Enforcement tier for a ToolGate.

    HARD_MANDATORY: failure terminates the action with no override path.
    SOFT_MANDATORY: failure pauses execution; a justification + evidence can
                    unlock continuation per the active policy.
    ADVISORY:       failure emits a warning; action proceeds unconditionally.
    """

    HARD_MANDATORY = "hard"
    SOFT_MANDATORY = "soft"
    ADVISORY = "advisory"


GateEvaluator = Callable[
    ["Branch", "ActionRequest", Any],
    Awaitable["GateResult"],
]


class GateResult(Element):
    """Immutable record of one gate evaluation.

    Every field is populated on both pass and fail.  This record is
    forwarded to the OperationContext (ADR-0050) and stored as an
    ImmutableEvidenceNode (ADR-0041).

    Fail-closed invariant: if the evaluator raised, ``passed`` is False
    and ``reason`` contains the exception message.  No exception propagates
    out of the gate executor.

    If enforcement is SOFT_MANDATORY and the gate failed, the record may
    later be amended with ``justification`` and ``justification_actor_id``
    when an override is accepted.  The amended record is a new frozen
    instance; the original is never mutated.
    """

    passed: bool
    gate_id: str
    enforcement: GateEnforcement
    reason: str                               # always populated, even on pass
    evaluated_at: float = Field(default_factory=time.monotonic)
    evidence_refs: Pile["EvidenceRef"] = Field(
        default_factory=lambda: Pile(item_type=Element, strict_type=False)
    )
    # Populated only when a SOFT gate is overridden:
    justification: str | None = None
    justification_actor_id: str | None = None

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=False,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    def with_justification(
        self,
        justification: str,
        actor_id: str,
    ) -> GateResult:
        """Return a new GateResult carrying override justification.

        The original record is preserved unchanged (immutable evidence).
        The returned record represents the override decision, not a mutation.
        """
        return self.model_copy(
            update={
                "evidence_refs": Pile(
                    collections=list(self.evidence_refs),
                    item_type=Element,
                    strict_type=False,
                ),
                "justification": justification,
                "justification_actor_id": actor_id,
            },
            deep=True,
        )


class ToolGate(Element):
    """A registered binary predicate applied before a governed tool call.

    ``evaluator`` is an async callable with the existing gate hook signature::

        async def evaluator(branch: Branch, tool_call: ActionRequest, context: object) -> GateResult:
            ...

    The adapter in ``lionagi/agent/gates/`` invokes this hook-shaped gate from
    ``Tool.preprocessor`` so it participates in the current Branch action flow
    without a parallel callback registry.

    ``owner`` is a free-form string naming the subsystem that owns this gate
    (e.g. ``"lionagi"``, ``"khive"``, ``"user"``).  Used in audit trails and
    error messages only; no behavioral effect.
    """

    gate_id: str
    enforcement: GateEnforcement
    evaluator: GateEvaluator = Field(exclude=True)
    description: str
    owner: str                                # "lionagi" | "khive" | "user"

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=False,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )
```

### 2. Gate Registration

Gates are managed through the existing `Branch` facade by mounting a `GateManager` on
`branch.acts` (`ActionManager`).  Two attachment points mirror the existing hook system:

- **Class-level gates** — attached to a tool's class via `@governed_tool(gates=[...])` (declared
  in [ADR-0043](ADR-0043-governed-tool-declaration.md)).  All instances of the class carry these
  gates; they run before action-level gates.
- **Action-level gates** — attached to a specific callable (one action on one tool) via
  `branch.attach_gate_to_action(tool, action, gate_id)`.

```python
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import Field

from lionagi.agent.gates.types import ToolGate
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


logger = logging.getLogger(__name__)


class GateBinding(Element):
    """Declarative attachment between a gate and a tool/action surface."""

    gate_id: str
    tool_name: str | None = None
    action: str | None = None
    tool_class_name: str | None = None

    def matches(self, *, tool: Tool, action: str) -> bool:
        class_name = tool.__class__.__name__
        return (
            bool(self.gate_id)
            and self.tool_class_name in (None, class_name)
            and self.tool_name in (None, tool.function)
            and self.action in (None, action)
        )


class GateManager(Element):
    """ActionManager-mounted collection of active ToolGates and bindings."""

    gates: Pile[ToolGate] = Field(
        default_factory=lambda: Pile(item_type=ToolGate, strict_type=True)
    )
    bindings: Pile[GateBinding] = Field(
        default_factory=lambda: Pile(item_type=GateBinding, strict_type=True)
    )

    def register_gate(self, gate: ToolGate) -> None:
        """Register a gate globally.  Duplicate gate_id replaces previous entry."""
        duplicate = next((g for g in self.gates if g.gate_id == gate.gate_id), None)
        if duplicate is not None:
            logger.warning("gate_manager: replacing existing gate %s", gate.gate_id)
            self.gates.exclude(duplicate)
        self.gates.include(gate)

    def attach_to_class(self, tool_class_name: str, gate_id: str) -> None:
        """Attach a registered gate to all actions on a tool class."""
        self.bindings.include(GateBinding(gate_id=gate_id, tool_class_name=tool_class_name))

    def attach_to_action(self, tool_name: str, action: str, gate_id: str) -> None:
        """Attach a registered gate to a specific tool action."""
        self.bindings.include(GateBinding(gate_id=gate_id, tool_name=tool_name, action=action))

    def gates_for(
        self,
        *,
        branch: Branch,
        tool_name: str,
        action: str,
    ) -> Pile[ToolGate]:
        """Return ordered gates applicable to a call.

        Order: class-level gates first (most general), then action-level
        gates (most specific).  Within each tier, registration order is
        preserved.  HARD gates are not sorted to the front here; the
        executor handles early-exit semantics.
        """
        tool = branch.acts.registry.get(tool_name)
        if tool is None:
            return Pile(item_type=ToolGate, strict_type=True)

        gates_by_id = {gate.gate_id: gate for gate in self.gates}
        ordered: list[ToolGate] = []
        seen: set[str] = set()
        class_bindings = [
            binding for binding in self.bindings if binding.tool_class_name is not None
        ]
        action_bindings = [
            binding for binding in self.bindings if binding.tool_class_name is None
        ]
        for binding in [*class_bindings, *action_bindings]:
            if binding.matches(tool=tool, action=action):
                gate = gates_by_id.get(binding.gate_id)
                if gate is not None and gate.gate_id not in seen:
                    seen.add(gate.gate_id)
                    ordered.append(gate)
        return Pile(collections=ordered, item_type=ToolGate, strict_type=True)


def ensure_gate_manager(branch: Branch) -> GateManager:
    """Attach gate state to the existing ActionManager instead of a callback registry."""
    manager = getattr(branch.acts, "gate_manager", None)
    if manager is None:
        manager = GateManager()
        branch.acts.gate_manager = manager
    return manager


# Branch facade methods added by the gate integration:
def register_gate(self: Branch, gate: ToolGate) -> None:
    ensure_gate_manager(self).register_gate(gate)


def attach_gate_to_action(self: Branch, tool_name: str, action: str, gate_id: str) -> None:
    ensure_gate_manager(self).attach_to_action(tool_name, action, gate_id)
```

### 3. Gate Executor and Fail-Closed Invariant

The executor runs all applicable gates and enforces the fail-closed invariant: if an evaluator
raises for any reason, the result is `GateResult(passed=False, ...)` — not a propagated exception,
not `passed=True`.  This is the most critical behavioral guarantee in this ADR.

```python
from __future__ import annotations

import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from pydantic import Field

from lionagi.agent.gates.manager import ensure_gate_manager
from lionagi.agent.gates.types import GateEnforcement, GateResult, ToolGate
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.messages.action_request import ActionRequest

if TYPE_CHECKING:
    from lionagi.protocols.action.tool import Tool
    from lionagi.session.branch import Branch

logger = logging.getLogger(__name__)


class GateExecutionResult(Element):
    """Aggregated outcome of running all gates for one tool call."""

    results: Pile[GateResult] = Field(
        default_factory=lambda: Pile(item_type=GateResult, strict_type=True)
    )
    hard_blocked: bool = False
    soft_blocked: bool = False
    blocking_gate_id: str | None = None

    @property
    def can_proceed(self) -> bool:
        return not self.hard_blocked and not self.soft_blocked


class GateExecutor(Element):
    """Evaluates all applicable gates for a tool call.

    Fail-closed: evaluator exceptions → passed=False.
    HARD gate failure → GateExecutionResult.hard_blocked=True; remaining
    gates are skipped.
    SOFT gate failure → execution pauses; a justification round-trip is
    required before continuation (see Section 4).
    ADVISORY gate failure → warning logged; result recorded; execution
    continues.
    """

    async def evaluate_all(
        self,
        *,
        branch: Branch,
        tool_call: ActionRequest,
        context: object | None = None,  # OperationContext (ADR-0050)
    ) -> GateExecutionResult:
        action = _action_name(tool_call)
        gate_manager = ensure_gate_manager(branch)
        gates = gate_manager.gates_for(
            branch=branch,
            tool_name=tool_call.function,
            action=action,
        )
        results = Pile(item_type=GateResult, strict_type=True)

        for gate in gates:
            result = await self._run_one(gate, branch, tool_call, context)
            results.include(result)
            self._record_result(branch, context, tool_call, result)

            if not result.passed:
                if result.enforcement == GateEnforcement.HARD_MANDATORY:
                    # Fail-closed: stop immediately, do not evaluate remaining gates.
                    return GateExecutionResult(
                        results=results,
                        hard_blocked=True,
                        soft_blocked=False,
                        blocking_gate_id=result.gate_id,
                    )
                if result.enforcement == GateEnforcement.SOFT_MANDATORY:
                    # Pause for justification; remaining gates skipped until override.
                    return GateExecutionResult(
                        results=results,
                        hard_blocked=False,
                        soft_blocked=True,
                        blocking_gate_id=result.gate_id,
                    )
                # ADVISORY: log warning, continue evaluating remaining gates.
                logger.warning(
                    "gate advisory failed: gate_id=%s tool=%s.%s reason=%s",
                    result.gate_id, tool_call.function, action, result.reason,
                )

        return GateExecutionResult(
            results=results,
            hard_blocked=False,
            soft_blocked=False,
            blocking_gate_id=None,
        )

    async def _run_one(
        self,
        gate: ToolGate,
        branch: Branch,
        tool_call: ActionRequest,
        context: object | None,
    ) -> GateResult:
        """Execute one gate evaluator.  Never raises — fail-closed."""
        try:
            result = gate.evaluator(branch, tool_call, context)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, GateResult):
                raise TypeError(f"Gate {gate.gate_id} returned {type(result)!r}")
            return result
        except BaseException as exc:
            # Fail-closed invariant: evaluator error → deny.
            logger.exception("gate evaluator raised (fail-closed deny): %s", gate.gate_id)
            return GateResult(
                passed=False,
                gate_id=gate.gate_id,
                enforcement=gate.enforcement,
                reason=f"evaluator raised: {type(exc).__name__}: {exc}",
                evaluated_at=time.monotonic(),
            )

    def _record_result(
        self,
        branch: Branch,
        context: object | None,
        tool_call: ActionRequest,
        result: GateResult,
    ) -> None:
        """Record gate evidence through OperationContext and Branch DataLogger."""
        if context is not None and hasattr(context, "record_gate_result"):
            context.record_gate_result(result)
        branch._log_manager.log(
            {
                "event": "tool_gate.evaluated",
                "branch_id": str(branch.id),
                "function": tool_call.function,
                "gate_result": result.to_dict(mode="json"),
            }
        )


def _action_name(tool_call: ActionRequest) -> str:
    args = tool_call.arguments or {}
    return str(args.get("action") or args.get("operation") or "")


def install_gate_preprocessor(branch: Branch, tool: Tool) -> None:
    """Run gates through Tool.preprocessor, the path ActionManager already invokes."""
    prior = tool.preprocessor
    prior_kwargs = dict(tool.preprocessor_kwargs or {})
    executor = GateExecutor()

    async def gated_preprocessor(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        if prior is not None:
            args = prior(args, **prior_kwargs)
            if inspect.isawaitable(args):
                args = await args

        tool_call = ActionRequest(
            content={"function": tool.function, "arguments": args},
            sender=branch.id,
            recipient=tool.id,
        )
        context = getattr(branch, "_active_operation_context", None)
        execution = await executor.evaluate_all(
            branch=branch,
            tool_call=tool_call,
            context=context,
        )
        if not execution.can_proceed:
            raise PermissionError(f"tool gate blocked: {execution.blocking_gate_id}")
        return args

    tool.preprocessor = gated_preprocessor
    tool.preprocessor_kwargs = {}
```

### 4. SOFT Gate Override Flow

When the executor returns `soft_blocked=True`, execution pauses before the tool action runs.
The override path is:

1. The branch surfaces the blocking `GateResult` to the calling context (agent, orchestrator,
   or human via [ADR-0046](ADR-0046-jit-tool-grant.md)).
2. The calling context invokes `branch.justify_gate(gate_id, justification, actor_id, evidence)`.
3. The branch's active `JustificationPolicy` evaluates the justification.  The policy is pluggable;
   the default policy accepts any non-empty justification from a registered actor.
4. If accepted: the original `GateResult` is preserved in the evidence chain; a new
   `GateResult` carrying `justification` and `justification_actor_id` is appended as a sibling
   evidence node.  Execution continues.
5. If rejected: a `GateResult(passed=False, reason="justification rejected", ...)` is appended.
   The action terminates with a DENY outcome recorded in the OperationContext.

```python
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from lionagi.agent.gates.types import GateEnforcement, GateResult
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

if TYPE_CHECKING:
    from lionagi.protocols.governance.evidence import EvidenceRef
    from lionagi.session.branch import Branch


async def justify_gate(
    branch: Branch,
    gate_id: str,
    justification: str,
    actor_id: str,
    evidence: Pile["EvidenceRef"] | None = None,
) -> bool:
    """Attempt to override a SOFT_MANDATORY gate failure.

    Returns True if the justification was accepted and execution may resume.
    Returns False if rejected; the branch records a DENY outcome.

    The justification and its acceptance/rejection both become immutable
    evidence nodes (ADR-0041) attached to the active OperationContext (ADR-0050).
    """
    evidence_refs = evidence or Pile(item_type=Element, strict_type=False)
    policy = getattr(branch, "_justification_policy", DefaultJustificationPolicy())
    accepted = await policy.evaluate(gate_id, justification, actor_id, evidence_refs)

    result = GateResult(
        passed=accepted,
        gate_id=gate_id,
        enforcement=GateEnforcement.SOFT_MANDATORY,
        reason="justification accepted" if accepted else "justification rejected",
        evaluated_at=time.monotonic(),
        evidence_refs=evidence_refs,
        justification=justification,
        justification_actor_id=actor_id,
    )

    ctx = getattr(branch, "_active_operation_context", None)
    if ctx is not None and hasattr(ctx, "record_gate_result"):
        ctx.record_gate_result(result)
    branch._log_manager.log(
        {
            "event": "tool_gate.justification",
            "branch_id": str(branch.id),
            "gate_result": result.to_dict(mode="json"),
        }
    )

    return accepted


class DefaultJustificationPolicy:
    """Accept any non-empty justification from any actor.

    Production deployments replace this with a policy that validates actor
    identity, checks justification length, and/or requires corroborating evidence.
    """

    async def evaluate(
        self,
        gate_id: str,
        justification: str,
        actor_id: str,
        evidence: Pile["EvidenceRef"],
    ) -> bool:
        return bool(justification.strip()) and bool(actor_id.strip())
```

### 5. Resolution Order

All applicable gates evaluate in this order.  The first HARD failure terminates; all ADVISORY
failures accumulate.  A SOFT failure pauses after the first occurrence (remaining gates do not
run until the override is resolved).

```text
1. Class-level hard gates      (attached to tool class via @governed_tool)
2. Class-level situational gates (conditional on args, still class-level registration)
3. Action-level hard gates     (attached to specific callable)
4. Action-level situational gates
```

Within each level, registration order is preserved.  Most-specific wins in the sense that
action-level gates can shadow class-level gates via `gate_id` deduplication, but all applicable
gates still run unless a HARD failure occurs.

### 6. Built-in Gates

lionagi ships four built-in gates.  All live under `lionagi/agent/gates/` and are
available for attachment without additional imports.

```python
# lionagi/agent/gates/builtins.py

from __future__ import annotations

import json
import urllib.parse
from typing import TYPE_CHECKING, Any

from lionagi.agent.gates.types import GateEnforcement, GateResult, ToolGate
from lionagi.agent.hooks import guard_destructive, guard_paths

if TYPE_CHECKING:
    from lionagi.protocols.messages.action_request import ActionRequest
    from lionagi.session.branch import Branch


def _args(tool_call: ActionRequest) -> dict[str, Any]:
    return tool_call.arguments or {}


# --- guard_destructive ---------------------------------------------------

async def gate_guard_destructive(
    branch: Branch,
    tool_call: ActionRequest,
    context: object | None,
) -> GateResult:
    args = _args(tool_call)
    try:
        await guard_destructive(tool_call.function, args.get("action", ""), args)
    except PermissionError as exc:
        return GateResult(
            passed=False,
            gate_id="guard_destructive",
            enforcement=GateEnforcement.HARD_MANDATORY,
            reason=str(exc),
        )
    return GateResult(
        passed=True,
        gate_id="guard_destructive",
        enforcement=GateEnforcement.HARD_MANDATORY,
        reason="no destructive pattern",
    )


GUARD_DESTRUCTIVE = ToolGate(
    gate_id="guard_destructive",
    enforcement=GateEnforcement.HARD_MANDATORY,
    evaluator=gate_guard_destructive,
    description="Block destructive shell commands with no override path.",
    owner="lionagi",
)


# --- guard_paths ---------------------------------------------------------

def make_guard_path_gate(
    *,
    allowed_paths: list[str] | None = None,
    denied_paths: list[str] | None = None,
    enforcement: GateEnforcement = GateEnforcement.HARD_MANDATORY,
) -> ToolGate:
    """Factory: adapt the existing guard_paths hook into a ToolGate."""
    path_hook = guard_paths(allowed_paths=allowed_paths, denied_paths=denied_paths)

    async def gate_guard_paths(
        branch: Branch,
        tool_call: ActionRequest,
        context: object | None,
    ) -> GateResult:
        args = _args(tool_call)
        try:
            await path_hook(tool_call.function, args.get("action", ""), args)
        except PermissionError as exc:
            return GateResult(
                passed=False,
                gate_id="guard_paths",
                enforcement=enforcement,
                reason=str(exc),
            )
        return GateResult(
            passed=True,
            gate_id="guard_paths",
            enforcement=enforcement,
            reason="path allowed",
        )

    return ToolGate(
        gate_id="guard_paths",
        enforcement=enforcement,
        evaluator=gate_guard_paths,
        description="Block or flag file access outside the designated workspace root.",
        owner="lionagi",
    )


# --- confirm_external_api_call -------------------------------------------

_ALLOWLISTED_API_HOSTS: frozenset[str] = frozenset({
    "api.openai.com",
    "api.anthropic.com",
    "api.cohere.com",
})


async def gate_confirm_external_api_call(
    branch: Branch,
    tool_call: ActionRequest,
    context: object | None,
) -> GateResult:
    args = _args(tool_call)
    url = args.get("url", "")
    if not url:
        return GateResult(
            passed=True,
            gate_id="confirm_external_api_call",
            enforcement=GateEnforcement.SOFT_MANDATORY,
            reason="no url argument",
        )
    host = urllib.parse.urlparse(url).hostname or ""
    allowed = host in _ALLOWLISTED_API_HOSTS
    return GateResult(
        passed=allowed,
        gate_id="confirm_external_api_call",
        enforcement=GateEnforcement.SOFT_MANDATORY,
        reason="host in allowlist" if allowed else f"host not in allowlist: {host}",
    )


CONFIRM_EXTERNAL_API_CALL = ToolGate(
    gate_id="confirm_external_api_call",
    enforcement=GateEnforcement.SOFT_MANDATORY,
    evaluator=gate_confirm_external_api_call,
    description="Require justification for outbound HTTP to non-allowlisted hosts.",
    owner="lionagi",
)


# --- warn_large_payload --------------------------------------------------

_PAYLOAD_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MB


async def gate_warn_large_payload(
    branch: Branch,
    tool_call: ActionRequest,
    context: object | None,
) -> GateResult:
    args = _args(tool_call)
    try:
        size = len(json.dumps(args).encode())
    except Exception:
        size = 0
    over = size > _PAYLOAD_LIMIT_BYTES
    return GateResult(
        passed=not over,
        gate_id="warn_large_payload",
        enforcement=GateEnforcement.ADVISORY,
        reason=f"payload {size} bytes exceeds {_PAYLOAD_LIMIT_BYTES}" if over else f"payload {size} bytes",
    )


WARN_LARGE_PAYLOAD = ToolGate(
    gate_id="warn_large_payload",
    enforcement=GateEnforcement.ADVISORY,
    evaluator=gate_warn_large_payload,
    description="Warn (non-blocking) when tool input arguments exceed 1 MB.",
    owner="lionagi",
)
```

### 7. User-Defined Gates

Framework consumers register custom gates via `branch.register_gate`:

```python
from lionagi.agent.gates.executor import install_gate_preprocessor
from lionagi.agent.gates.types import GateEnforcement, GateResult, ToolGate


async def require_ticket_number(branch, tool_call, context) -> GateResult:
    """Example: require a JIRA ticket in the operation context metadata."""
    meta = getattr(context, "metadata", {}) or {}
    ticket = meta.get("ticket", "")
    ok = bool(ticket and ticket.startswith("PROJ-"))
    return GateResult(
        passed=ok,
        gate_id="require_ticket_number",
        enforcement=GateEnforcement.SOFT_MANDATORY,
        reason="ticket present" if ok else "no PROJ-* ticket in operation context",
    )


my_gate = ToolGate(
    gate_id="require_ticket_number",
    enforcement=GateEnforcement.SOFT_MANDATORY,
    evaluator=require_ticket_number,
    description="Require a PROJ-* ticket reference before any write operation.",
    owner="user",
)

# Register globally on the branch and attach to a specific action:
branch.register_gate(my_gate)
branch.attach_gate_to_action("editor", "write", "require_ticket_number")
install_gate_preprocessor(branch, branch.acts.registry["editor"])
```

### 8. Relationship to Existing Primitives

The built-in `PermissionPolicy` and `guard_*` hooks remain in place.  They are not deprecated by
this ADR.  The migration path is additive:

- `guard_destructive` hook → `GUARD_DESTRUCTIVE` gate.  Both may coexist; the hook runs first
  as a pre-hook; the gate runs as part of gate evaluation.  In a future release the hook form
  will be soft-deprecated once gate attachment is the recommended path for governed tools.
- `PermissionPolicy` continues to handle allow/deny/escalate pattern matching.  It operates
  before gate evaluation in the call stack.  Gates add tier semantics and evidence production
  on top of the policy layer, not as a replacement.

### 9. Fail-Closed is an Invariant, Not a Convention

This point is sufficiently important to state explicitly and separately from the executor code.

> If a gate's evaluator raises any exception for any reason — including import errors, network
> timeouts, internal assertion failures, and `asyncio.CancelledError` — the gate result is
> `GateResult(passed=False, ...)`.  The exception is logged at ERROR level.  It is never
> re-raised, never silently swallowed into a pass, and never converted into an ADVISORY result
> regardless of the gate's declared enforcement tier.

This invariant means a misconfigured or crashing gate acts as a deny, not as an open door.
A gate that is supposed to check network access but fails to import its HTTP library blocks the
call rather than allowing it through.  This is the correct behavior for a security primitive.

---

## Consequences

**Positive**

- Every gate evaluation produces a `GateResult` regardless of pass or fail.  The operation record
  is complete: no tool call can execute without evidence that its gates ran.
- The three-tier model gives framework consumers a vocabulary for expressing real distinctions:
  irrevocable safety rules (HARD), overridable business rules (SOFT), informational checks
  (ADVISORY).  Previously this required separate systems.
- Fail-closed-by-default eliminates an entire class of "gate crashes → action proceeds"
  vulnerabilities.
- SOFT gate justifications become part of the evidence chain (via ADR-0041), making overrides
  auditable rather than invisible.
- The resolution order (class-level first, then action-level) mirrors the decorator stack
  familiar to Python developers and is predictable without reading framework internals.

**Negative**

- Latency for sessions with many gates: every tool call evaluates all applicable gates serially
  (HARD gates short-circuit on first failure; ADVISORY and SOFT gates may accumulate). For most
  agent sessions the gate count is small (2-5), making this negligible. Sessions with 20+ gates
  may see 5-15 ms overhead per call; profiling will determine whether parallel evaluation is
  warranted.
- Authoring gates requires understanding the `OperationContext` and `ActionRequest` interfaces
  (ADR-0050 and `lionagi/protocols/messages/`).  This is more involved than writing a hook that
  inspects `args: dict`.
- SOFT gate overrides introduce a synchronous pause in async tool execution.  Orchestrators that
  route justification requests to humans (ADR-0046) must handle the suspension correctly.
- Classifying a gate into the correct enforcement tier is a judgment call.  Misclassifying a
  safety requirement as SOFT introduces a gap; misclassifying a business rule as HARD removes
  operational flexibility.

---

## Non-Goals

Explicitly out of scope:

- **Probabilistic gating**: gates return `bool`.  Confidence-weighted authorization belongs in
  `Claim.confidence` (ADR-0039) and `StateReason.confidence` (ADR-0033), not in gate results.
  Threshold-based authorization is explicitly rejected (see prior research, Alternative 1).
- **Gate composition DSL**: there is no `AND(gate_a, gate_b)` or `OR(gate_a, gate_b)` syntax.
  Composite logic is expressed by calling other gates inside an evaluator function.
- **Multi-tenant policy stitching**: resolving gate inheritance across tenant hierarchies,
  overriding organizational gates at the workspace level, and merging gate registries from
  multiple policy sources are KHive-layer concerns addressed in ADR-0052.
- **Human-approval workflows**: routing a SOFT override to a human approver UI, timeout
  handling, and async approval state machines are addressed in ADR-0046 (JIT Tool Grant).
- **Gate versioning and migration**: upgrading a gate's enforcement tier across live sessions,
  retiring a gate_id, and backfilling historical evidence with updated gate metadata are
  operational concerns not addressed here.
- **Cross-agent gate delegation**: an agent delegating gate-pass authority to a sub-agent is
  addressed by the Agent Charter (ADR-0047) and Segregation of Duties (ADR-0048).

---

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Single allow/deny mechanism (no tiers) | Real deployments distinguish irrevocable safety requirements from overridable business rules. Flattening both into HARD removes the escalation path that makes SOFT useful; flattening both into SOFT allows safety-critical gates to be bypassed with a justification string. |
| Confidence-score gates (0.0–1.0) | Authorization is a factual question with a binary answer. Confidence scores shift the authorization decision to the threshold-chooser, introduce legal ambiguity ("the gate was 73% sure"), and enable threshold-drift attacks. Rejected per prior research Alternative 1. |
| Hook-only enforcement (existing pattern) | Hooks (`guard_destructive`, `guard_paths`) raise `PermissionError` with no tiered semantics, no evidence production, and no override path. They cannot express SOFT or ADVISORY distinctions. They produce no `GateResult` that persists in the operation record. |
| Auto-registration via `__init_subclass__` | prior research notes this pattern, but lionagi's tool model uses registered callables rather than class hierarchies. Explicit `register_gate` calls match the existing `register_tools` pattern and make attachment auditable. |
| Gate evaluation in parallel | Parallel evaluation breaks the fail-fast semantics of HARD gates: if HARD gate A and SOFT gate B both run concurrently and A fails, B may have already started an I/O side-effect. Serial evaluation with short-circuit on HARD failure is correct and the latency cost is acceptable. |

---

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — `GateResult` instances are stored as immutable evidence nodes; hash-chain integrity applies
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate consumes aggregated gate results as part of the signed process record
- [ADR-0043](ADR-0043-governed-tool-declaration.md) — `@governed_tool` declares which gates are attached at the class level; HARD gates declared there cannot be absent at runtime
- [ADR-0045](ADR-0045-break-glass-protocol.md) — documents what happens when a HARD gate result is overridden at the system level (DEGRADED defensibility mode)
- [ADR-0046](ADR-0046-jit-tool-grant.md) — the JIT grant protocol handles human-in-the-loop approval for SOFT gate overrides
- [ADR-0047](ADR-0047-agent-charter.md) — charters bind agents to specific gate sets; a gate not declared in the charter cannot be bypassed
- [ADR-0050](ADR-0050-operation-context.md) — `OperationContext` is the active assertion that receives `GateResult` records at evaluation time
- [ADR-0052](ADR-0052-policy-resolution.md) — most-specific-wins resolution for gate registries across tenant hierarchies (KHive layer)
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — `EvidenceRef` (8 kinds) is the shared substrate; `GateResult.evidence_refs` uses this type
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — `Claim.confidence` is where probabilistic reasoning lives; explicitly not in gates
- prior governance research `01_design/008-policy-gates/ADR-008-policy-gates.md` — source pattern (three-tier model, binary semantics, HashiCorp Sentinel inspiration)
- `lionagi/agent/permissions.py` — existing `PermissionPolicy`; not deprecated, gates layer on top
- `lionagi/agent/hooks.py` — existing `guard_destructive`, `guard_paths`; migration path described in Section 8
