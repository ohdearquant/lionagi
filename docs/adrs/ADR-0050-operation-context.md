# ADR-0050: Operation Context — Active Assertion in Evidence

**Status**: Proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md), [ADR-0043](ADR-0043-governed-tool-declaration.md), [ADR-0044](ADR-0044-tool-gates.md), [ADR-0047](ADR-0047-agent-charter.md)
**Related**: [ADR-0042](ADR-0042-task-certificate.md), [ADR-0045](ADR-0045-break-glass-protocol.md), [ADR-0046](ADR-0046-jit-tool-grant.md), [ADR-0049](ADR-0049-log-tier-governance.md), [ADR-0051](ADR-0051-tool-registry-allowlists.md), [ADR-0052](ADR-0052-policy-resolution.md), [ADR-0033](ADR-0033-unified-entity-state-model.md), [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)

---

## Context

lionagi's `Branch` today holds session-level state across its four managers: `MessageManager`
(message history), `ActionManager` (registered tools), `iModelManager` (provider references),
and `DataLogger` (log entries). When `branch.operate()` dispatches a tool call, the tool
receives its arguments and returns a result. The `DataLogger` may record a `Log` entry. Nothing
else is captured about the operation as a unit.

The structural gap is that two evidence nodes emitted by different operations — or even by the
same operation running twice — are indistinguishable from each other without external context.
There is no record of which policy version was active, which gates passed or failed, which agent
charter was in force, or what caused this operation to run. An audit question such as "what
constraints were active when the agent wrote to this file at 14:32?" cannot be answered by
inspecting the evidence node itself. The evidence node is a fact about what happened; without
operation context it is not self-describing proof of how it was authorized to happen.

This violates cross-cutting principle #2: **evidence is first-class, not logs**. A log entry
records what happened operationally. Evidence carries the policy version, gate results, and
authorization context active *at execution time* — this is what makes evidence admissible as
proof. Without operation context embedded in every evidence node, the evidence carries no more
authority than an application log.

There is also a correctness hazard specific to async code. lionagi uses asyncio throughout. If
operation-level state (gate results, causation chain, consumed permit tokens) were accumulated
in a shared mutable session-level structure, concurrent coroutines within the same session would
see each other's partially-accumulated state. The result would be either incorrect evidence
(gate results from operation A appear in evidence for operation B) or a race condition requiring
explicit locking.

### The applicable prior governance research insight

prior research establishes a two-tier context system: a `ServiceContext` initialized once at
service startup holding long-lived references (charter, gate registry, policy version, tenant
binding), and a `RequestContext` created per-request and propagated explicitly through every
governed function as a named parameter. The key design insight is the "active assertion" pattern:
the `RequestContext.to_evidence_data()` method serializes the *full policy state in force at
execution time* into every evidence payload. Evidence becomes self-describing: an auditor
inspecting a node years later can determine exactly what policy version governed the execution,
which gates ran and what they returned, and which actor initiated the call — without needing to
consult current system state. The context IS the proof.

Translated to lionagi: `ServiceContext` is initialized at `Branch` creation time; it holds
the gate registry, charter, resolved policy bundle, and knowledge store references. An
`OperationContext` is created fresh for each `branch.operate()` call or governed tool execution.
It is immutable. Gate results, permit tokens, and evidence node IDs are accumulated via
transform methods that return new instances. Every evidence node emitted during the operation
(per ADR-0041) receives a snapshot of the `OperationContext` at the moment of emission.

### Why lionagi needs this

Consider a KHive agent that calls `branch.operate()` three times in quick succession: one call
reads configuration, one writes a checkpoint, one invokes a sub-agent. All three run
concurrently on the asyncio event loop. The write operation passes a `guard_paths` HARD gate
and a `confirm_path_outside_workspace` SOFT gate with a provided justification. The read
operation passes `guard_paths` but has no SOFT gate. Without `OperationContext`, the evidence
node for the write has no record of the SOFT gate result or the justification that authorized
it. If the justification is later disputed, the evidence is silent. With `OperationContext`, the
write's evidence node contains `gate_results: (GateResult(id="confirm_path...", passed=True,
justification="checkpoint required before migration"),)` and `policy_version_active:
"2026-05-1.3"` at the moment of emission — independently of what the other two concurrent
operations did or what the policy version is today.

---

## Decision

Introduce a two-tier context system: `ServiceContext` (per-session, initialized at `Branch`
creation) and `OperationContext` (per-operation, immutable, transforms create new instances).
Both are propagated explicitly as function arguments. Neither uses thread-locals or global state.
Every evidence node emitted during a governed operation embeds the `OperationContext` active at
the moment of emission as its active assertion.

---

### 1. `ServiceContext` — Per-Session Long-Lived References

`ServiceContext` is created once when a `Branch` is initialized in governed mode. It holds
references that do not change over the session lifetime: the gate registry (ADR-0044), the agent
charter and its version (ADR-0047), the resolved policy bundle version (ADR-0052), and the
knowledge store (ADR-0039).

```python
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import ConfigDict, Field

from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger
from lionagi.protocols.messages.manager import MessageManager
from lionagi.service.manager import iModelManager

if TYPE_CHECKING:
    from lionagi.session.branch import Branch
    from lionagi.protocols.governance.gates import GateRegistry
    from lionagi.protocols.governance.charter import AgentCharter
    from lionagi.protocols.knowledge.store import KnowledgeStore


class ServiceContext(Element):
    """Initialized once at Branch creation in governed mode.

    Holds long-lived references (gate registry, charter, policy version,
    knowledge store) plus references to the existing Branch managers. Every
    OperationContext created during the session carries this object so
    per-operation creation does not create parallel context stores.
    """

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    branch_id: str
    session_id: str
    agent_id: str
    gate_registry: "GateRegistry" = Field(exclude=True)
    charter: "AgentCharter" = Field(exclude=True)
    policy_release_version: str
    knowledge_store: "KnowledgeStore" = Field(exclude=True)
    message_manager: MessageManager = Field(exclude=True)
    action_manager: ActionManager = Field(exclude=True)
    imodel_manager: iModelManager = Field(exclude=True)
    data_logger: DataLogger = Field(exclude=True)
    started_at: float = Field(default_factory=time.time)

    @classmethod
    def from_branch(
        cls,
        branch: "Branch",
        *,
        gate_registry: "GateRegistry",
        charter: "AgentCharter",
        policy_release_version: str,
        knowledge_store: "KnowledgeStore",
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> "ServiceContext":
        """Bind governance context to Branch's existing manager surfaces."""
        return cls(
            branch_id=str(branch.id),
            session_id=session_id or str(branch.id),
            agent_id=agent_id or str(branch.user or branch.id),
            gate_registry=gate_registry,
            charter=charter,
            policy_release_version=policy_release_version,
            knowledge_store=knowledge_store,
            message_manager=branch.msgs,
            action_manager=branch.acts,
            imodel_manager=branch.mdls,
            data_logger=branch._log_manager,
        )
```

`ServiceContext` is frozen: no field can be changed after initialization. A new session always
produces a new `ServiceContext`. There is no path to mutate the gate registry or charter
mid-session; changes require starting a new session.

---

### 2. `GateResult` — Atomic Gate Outcome

`GateResult` is the unit stored in `OperationContext.gate_results`. It is a frozen record of one
gate's evaluation outcome, captured at evaluation time and never modified.

```python
from __future__ import annotations

import time
from typing import Literal

from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element


class GateResult(Element):
    """Immutable record of a single gate evaluation (ADR-0044).

    Accumulated into ``OperationContext.gate_results`` via
    ``with_gate_result()``; serialized into every evidence node.
    """

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    gate_id: str
    gate_tier: Literal["HARD", "SOFT", "ADVISORY"]
    passed: bool
    reason: str
    justification: str | None = None
    evaluated_at: float = Field(default_factory=time.time)
```

---

### 3. `OperationContext` — Per-Operation Immutable Active Assertion

`OperationContext` is created fresh at the start of each governed operation. Its fields are
immutable. Gate results, permit tokens, and evidence node IDs are accumulated via transform
methods that return new instances; the original is never modified.

```python
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


class PermitTokenUse(Element):
    """JIT permit token consumed during this operation (ADR-0046)."""

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    token_id: str
    consumed_at: float = Field(default_factory=time.time)


class EvidenceEmission(Element):
    """Evidence node emitted during this operation (ADR-0041)."""

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    evidence_id: str
    emitted_at: float = Field(default_factory=time.time)


class OperationContext(Element):
    """Per-operation context. Immutable. Active assertion of state at execution time.

    Created once per governed operation.  Gate results, permit tokens, and
    evidence IDs are accumulated via transform methods (``with_*``) that
    return new instances — the original is never mutated.  The final snapshot
    is serialized into every evidence node via ``to_evidence_data()``.
    """

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    # Identity
    service_context: ServiceContext = Field(exclude=True)
    actor_id: str
    parent_operation_id: str | None = None  # causation chain; None = top-level
    initiated_at: float = Field(default_factory=time.time)
    actor_type: Literal["agent", "user", "system"] = "agent"

    # Accumulated during operation (Pile copies grown via transforms):
    gate_results: Pile[GateResult] = Field(
        default_factory=lambda: Pile(item_type=GateResult, strict_type=True)
    )
    permit_tokens_consumed: Pile[PermitTokenUse] = Field(
        default_factory=lambda: Pile(item_type=PermitTokenUse, strict_type=True)
    )
    evidence_emitted: Pile[EvidenceEmission] = Field(
        default_factory=lambda: Pile(item_type=EvidenceEmission, strict_type=True)
    )

    # Active assertion — captured at creation, not updated later:
    policy_version_active: str = ""
    charter_version_active: int | str = 0

    operation_name: str = ""               # optional audit label

    @property
    def operation_id(self) -> str:
        """Element id is the operation id used in evidence and causation."""
        return str(self.id)

    # ------------------------------------------------------------------
    # Immutable transforms
    # ------------------------------------------------------------------

    def with_gate_result(self, result: GateResult) -> "OperationContext":
        """Return a new context with ``result`` appended to ``gate_results``."""
        return self.model_copy(
            update={
                "gate_results": Pile(
                    collections=[*self.gate_results, result],
                    item_type=GateResult,
                    strict_type=True,
                )
            }
        )

    def with_causation(self, parent_id: str) -> "OperationContext":
        """Return a new context with ``parent_operation_id`` set."""
        return self.model_copy(update={"parent_operation_id": parent_id})

    def with_evidence(self, evidence_id: str) -> "OperationContext":
        """Return a new context with ``evidence_id`` appended to ``evidence_emitted``."""
        emission = EvidenceEmission(evidence_id=evidence_id)
        return self.model_copy(
            update={
                "evidence_emitted": Pile(
                    collections=[*self.evidence_emitted, emission],
                    item_type=EvidenceEmission,
                    strict_type=True,
                )
            }
        )

    def with_permit_token(self, token_id: str) -> "OperationContext":
        """Return a new context with ``token_id`` appended to ``permit_tokens_consumed``."""
        token_use = PermitTokenUse(token_id=token_id)
        return self.model_copy(
            update={
                "permit_tokens_consumed": Pile(
                    collections=[*self.permit_tokens_consumed, token_use],
                    item_type=PermitTokenUse,
                    strict_type=True,
                )
            }
        )

    # ------------------------------------------------------------------
    # Active assertion serialization
    # ------------------------------------------------------------------

    def to_evidence_data(self) -> dict[str, Any]:
        """Serialize the context as an active assertion payload.

        Embedded verbatim into every evidence node emitted during this
        operation.  An auditor reading the evidence node in isolation
        can determine the full authorization context — policy version,
        gate outcomes, actor, causation chain — without querying the
        live system.  Returns a JSON-safe dict.
        """
        return {
            "operation_id": self.operation_id,
            "parent_operation_id": self.parent_operation_id,
            "branch_id": self.service_context.branch_id,
            "session_id": self.service_context.session_id,
            "agent_id": self.service_context.agent_id,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "initiated_at": self.initiated_at,
            "policy_version_active": self.policy_version_active,
            "charter_version_active": self.charter_version_active,
            "operation_name": self.operation_name,
            "message_ids": [
                str(message_id)
                for message_id in self.service_context.message_manager.progression
            ],
            "registered_tools": sorted(self.service_context.action_manager.registry),
            "chat_model": getattr(self.service_context.imodel_manager.chat, "model", None),
            "parse_model": getattr(self.service_context.imodel_manager.parse, "model", None),
            "gate_results": [gr.to_dict(mode="json") for gr in self.gate_results],
            "permit_tokens_consumed": [
                token.to_dict(mode="json") for token in self.permit_tokens_consumed
            ],
            "evidence_emitted": [
                evidence.to_dict(mode="json") for evidence in self.evidence_emitted
            ],
        }
```

---

### 4. `OperationContext` Factory

Context creation is centralized in a single factory function to ensure consistent population of
`policy_version_active` and `charter_version_active` from the `ServiceContext` at creation time.

```python
from lionagi.session.branch import Branch


class MissingServiceContextError(RuntimeError):
    """Raised when governed operation context is requested on an ungoverned Branch."""


def create_operation_context(
    branch: Branch,
    *,
    actor_id: str | None = None,
    actor_type: str = "agent",
    parent_operation_id: str | None = None,
    operation_name: str = "",
) -> OperationContext:
    """Create a fresh OperationContext for a new governed operation.

    Captures ``policy_version_active`` and ``charter_version_active``
    from Branch's attached ``ServiceContext`` at this instant. Evidence
    from this operation will always carry the version that was active at
    start, regardless of later session hot-reloads.
    """
    service_context = branch.metadata.get("governance_service_context")
    if not isinstance(service_context, ServiceContext):
        raise MissingServiceContextError(
            "Governed operation requires ServiceContext on Branch.metadata"
        )

    ctx = OperationContext(
        service_context=service_context,
        parent_operation_id=parent_operation_id,
        actor_id=actor_id or str(branch.user or branch.id),
        actor_type=actor_type,
        operation_name=operation_name,
        policy_version_active=service_context.policy_release_version,
        charter_version_active=getattr(service_context.charter, "version", 0),
    )
    branch._log_manager.log(
        {"event": "operation_context_created", "context": ctx.to_evidence_data()}
    )
    return ctx
```

---

### 5. Explicit Propagation — Not Thread-Local, Not Global

Context is passed as a named argument to every governed function. There is no global registry,
no thread-local, and no `contextvars.ContextVar` for the `OperationContext` itself.

```python
# CORRECT: explicit parameter, async-safe
import time
from collections.abc import Awaitable, Callable
from typing import Any

from lionagi.agent.config import AgentConfig
from lionagi.agent.hooks import guard_destructive, guard_paths
from lionagi.session.branch import Branch

PreHook = Callable[
    [str, str, dict[str, Any]],
    Awaitable[dict[str, Any] | None],
]


async def _run_gate(
    branch: Branch,
    hook: PreHook,
    tool_name: str,
    action: str,
    kwargs: dict[str, Any],
    ctx: OperationContext,
) -> tuple[GateResult, OperationContext, dict[str, Any]]:
    """Evaluate an existing lionagi pre-hook and return result, ctx, args."""
    hook_args = {**kwargs, "operation_context": ctx.to_evidence_data()}
    try:
        updated_args = await hook(tool_name, action, hook_args)
    except Exception as exc:
        gate_result = GateResult(
            gate_id=getattr(hook, "__name__", hook.__class__.__name__),
            gate_tier="HARD",
            passed=False,
            reason=str(exc),
            evaluated_at=time.time(),
        )
        ctx = ctx.with_gate_result(gate_result)
        branch._log_manager.log(gate_result)
        raise

    gate_result = GateResult(
        gate_id=getattr(hook, "__name__", hook.__class__.__name__),
        gate_tier="HARD",
        passed=True,
        reason="hook passed",
        evaluated_at=time.time(),
    )
    ctx = ctx.with_gate_result(gate_result)
    branch._log_manager.log(gate_result)
    return gate_result, ctx, updated_args if updated_args is not None else hook_args


# Built-in hooks from lionagi.agent.hooks are registered through AgentConfig
# and wired into Tool.preprocessor by create_agent()/CodingToolkit.
config = AgentConfig.coding()
config.pre("bash", guard_destructive)
config.pre("editor", guard_paths(allowed_paths=["/workspace"]))


# WRONG: global or thread-local lookup
async def _run_gate_wrong(gate_id: str, tool_name: str, kwargs: dict) -> GateResult:
    ctx = _get_current_context()   # Anti-pattern: breaks under concurrent coroutines
    ...
```

The rationale: asyncio does not guarantee that a `ContextVar` set in coroutine A is invisible
to coroutine B if B was created with `asyncio.create_task()` before A set the variable. Two
concurrent `branch.operate()` calls would each create their own asyncio task; `ContextVar`
inheritance means a child task sees the parent's context at the moment of creation, not live
updates. This makes `ContextVar` safe for the bypass-prevention flag (ADR-0043, which is
set-once before calling the handler), but unsafe for the accumulating `OperationContext` where
each gate evaluation produces a new instance. Explicit propagation has one disadvantage —
function signatures grow a `ctx: OperationContext` parameter — and one decisive advantage:
correctness under all concurrency patterns without special reasoning.

---

### 6. Pipeline Integration — How `OperationContext` Flows Through ADR-0043

The eight-phase enforcement pipeline in `ActionManager.execute_governed()` (ADR-0043) creates,
accumulates, and finalizes the `OperationContext`:

| Phase | Pipeline action | OperationContext interaction |
|-------|-----------------|------------------------------|
| Phase 1 | Normalize inputs via `options_schema` | `ctx` not yet created |
| **Phase 2** | **Validate context exists** | `ctx = create_operation_context(service_ctx, actor_id=...)` — creation |
| Phase 3 | Resolve applicable gates from registry | `ctx` passed to `_resolve_gates(meta, fn_name, ctx)` |
| Phase 4 | Execute HARD gates | Each gate: `gate_result, ctx = await _run_gate(gate_id, ..., ctx)` |
| Phase 5 | Execute SOFT gates | Same pattern; on bypass: `ctx = ctx.with_permit_token(token_id)` if JIT |
| Phase 6 | Execute ADVISORY gates | Same pattern |
| Phase 7 | Execute handler inside pipeline context | `ctx` snapshot passed to handler via `_enter_pipeline(op_ctx_id=ctx.operation_id)` |
| **Phase 8** | **Emit evidence node** | `evidence_id = await _emit_success_evidence(..., ctx=ctx)`; then `ctx = ctx.with_evidence(evidence_id)` |

After Phase 8, `ctx` is the final snapshot: it contains all gate results from Phases 4–6, all
permit tokens consumed in Phase 5, and the evidence node ID from Phase 8. The Phase 8 evidence
node embeds `ctx.to_evidence_data()` as its active assertion payload.

On a HARD gate failure in Phase 4, the pipeline emits a failure evidence node carrying the
partial `ctx` (with gate results up to the failure point), then raises `GateBlockedError`.
The partial context is not discarded — the failure evidence is as important as the success
evidence for the audit trail.

---

### 7. Evidence Embedding — The Active Assertion

Every `ImmutableEvidenceNode` (ADR-0041) emitted during a governed operation includes the
serialized `OperationContext` as a top-level field in its domain data. This is the active
assertion: the evidence node is self-describing proof of what was in force when it was sealed.

```python
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, Field

from lionagi.protocols.governance.evidence import ImmutableEvidenceNode


class ToolExecutionEvidence(ImmutableEvidenceNode):
    """Evidence node for a governed tool execution.

    ``operation_context_data`` carries ``ctx.to_evidence_data()`` and IS
    included in the content hash — the policy state that authorized the
    execution is part of the evidence.  ``raw_inputs`` is excluded via
    ``_sensitive_fields``: tool arguments may contain secrets.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    tool_name: str = ""
    evidence_type: str = ""
    outcome: str = ""          # "success" | "gate_blocked" | "execution_error"
    justification: str | None = None

    # The active assertion: full context serialized at emission time
    operation_context_data: dict[str, Any] = Field(default_factory=dict)

    # Excluded from hashes and audit exports:
    raw_inputs: dict[str, Any] | None = Field(default=None, exclude=True)

    _sensitive_fields: ClassVar[set[str]] = {"raw_inputs"}
```

An auditor reading this node in isolation — without access to the live system — sees the
complete authorization context: policy version, all gate outcomes, actor identity, and
causation chain. The evidence is self-describing by construction.

---

### 8. Causation Chain

When a governed operation triggers a child operation (for example, a tool call that internally
invokes another governed tool via a sub-branch, or a `FlowOp` node calling a downstream tool),
the child's `OperationContext` is created with `parent_operation_id` pointing to the parent's
`operation_id`. This forms a tree:

```text
branch.operate() → OperationContext(id="op-A", parent=None)
  └─ write_file tool → OperationContext(id="op-B", parent="op-A")
       └─ validate_path tool → OperationContext(id="op-C", parent="op-B")
```

Every evidence node in the tree carries its own `operation_id` and `parent_operation_id`.
An audit query reconstructs the full causation tree by following `parent_operation_id` links
from any leaf to the root — no separate tracing infrastructure required. The field is frozen
at creation; there is no mechanism to set `parent_operation_id` after the fact.

---

## Consequences

**Positive**

- **Every evidence node is independently interpretable**: the active assertion payload makes
  each node self-describing. An auditor does not need to query the live system to understand
  what policy was in force when a given tool call ran. This directly satisfies cross-cutting
  principle #2.
- **Async-safe accumulation**: immutable transforms mean concurrent coroutines accumulate their
  gate results in separate object chains. No locks required; no cross-contamination between
  concurrent operations.
- **Causation tree from evidence alone**: `parent_operation_id` links stored in evidence nodes
  allow full causation reconstruction without a separate tracing infrastructure.
- **Policy version captured at creation**: `policy_version_active` and `charter_version_active`
  are snapshotted at `create_operation_context()` time. A hot-reload or charter revision
  mid-session does not retroactively alter evidence from operations that started before it.
- **Fail-closed for missing context**: `ActionManager.execute_governed()` Phase 2 raises
  `MissingOperationContextError` if `ctx` is absent and the tool is not `skip_evidence=True`.
  The pipeline cannot silently proceed without a context; ambiguity closes the gate.
- **Testability**: test code creates a `ServiceContext` with mock gate registry and charter,
  then constructs `OperationContext` instances directly. No global state to set up or tear down.

**Negative**

- **Function signature pollution**: every governed function in the pipeline gains a
  `ctx: OperationContext` parameter. For simple library-mode callers not using governed tools,
  this parameter never appears. For governed-mode code, it is present at every gate evaluation
  site. The verbosity is intentional; it makes dependencies explicit.
- **Shallow copy per transform**: each `with_gate_result()`, `with_evidence()`, or
  `with_permit_token()` call produces a new frozen `Element` model. For an operation with ten gates,
  this is ten allocations of the `OperationContext` Pydantic model. The overhead is measurable in
  micro-benchmarks but irrelevant at the scale of gate evaluation (each gate is an async call).
- **`ServiceContext` must be initialized**: ungoverned library-mode branches do not create a
  `ServiceContext`. Adding governance to an existing branch requires providing a gate registry
  and charter, which are new dependencies. Migration is explicit, not zero-config.

---

## Non-Goals

Explicitly out of scope:

- **Implicit context via thread-locals or `contextvars`**: deliberately rejected (see §5). The
  accumulating `OperationContext` is unsafe for implicit propagation under asyncio concurrency.
  `contextvars` is used only for the bypass-prevention flag (ADR-0043), which is set-once, not
  accumulated.
- **Context for ungoverned tools**: tools not decorated with `@governed_tool` (ADR-0043) and
  invoked via `execute_raw()` do not receive an `OperationContext`. Library-mode callers
  using plain registered callables are unaffected.
- **Cross-process context propagation**: distributing `OperationContext` across process
  boundaries (e.g., serializing `operation_id` into an HTTP header for a remote tool call and
  reconstructing the causation chain at the receiver) is a KHive concern. Within a single
  lionagi session, context propagation is entirely in-process.
- **Multi-tenant context isolation**: ensuring that `ServiceContext` and `OperationContext`
  instances from different tenants cannot be interchanged requires storage-layer row security
  and tenant-scoped session initialization. This is KHive v1 territory.
- **Real-time context streaming**: broadcasting the accumulated `OperationContext` to an
  external observer as gates are evaluated (for live dashboards) is out of scope. The final
  snapshot is embedded in evidence; intermediate states are not surfaced.
- **Context outside the governed pipeline**: operations that run outside `execute_governed()`
  (break-glass paths in ADR-0045, ungoverned tools, direct `Branch` state manipulation) do not
  produce an `OperationContext`. Break-glass paths produce their own evidence records defined
  in ADR-0045.

---

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| **Thread-local storage** (`threading.local()`) | Incompatible with asyncio: asyncio runs coroutines on a single thread, so a thread-local shared by all concurrent coroutines would conflate their gate results. Rejected unconditionally for async code. |
| **`contextvars.ContextVar` for the accumulating context** | `ContextVar` is inherited by child tasks at creation time; subsequent updates are not visible to siblings. If coroutine A creates a child task and then appends a gate result via a `ContextVar`, the child does not see the update. Accumulating mutable state via `ContextVar` requires a `ContextVar[list]` with `.get()` and `.append()` — which reintroduces the race condition. `ContextVar` is suitable for set-once flags (ADR-0043's pipeline guard), not for accumulation. |
| **Global registry keyed by `operation_id`** | A `dict[str, OperationContext]` singleton avoids parameter passing. It introduces a global write point that requires locking under concurrent operations, a cleanup protocol (when does an entry expire?), and an implicit dependency invisible in function signatures. Rejected per explicit-propagation principle from prior research. |
| **Mutable `OperationContext` with per-field locks** | Locking individual fields (`gate_results_lock: asyncio.Lock`) would allow concurrent accumulation without a global mutex. It adds complexity disproportionate to the problem: the immutable-transform approach achieves the same result with simpler reasoning and no lock hierarchy. Rejected per prior research. |
| **No `OperationContext`; embed policy version in each `EvidenceRef` directly** | `EvidenceRef` (ADR-0033) could carry individual `policy_version` and `actor_id` fields. This duplicates the same data into every node without establishing the link between nodes that belong to the same operation. The causation chain, the gate accumulation, and the permit token record all require a shared anchor — which IS the `OperationContext`. Individual fields are insufficient. |
| **Collapse to single-tier (no `ServiceContext`)** | A flat `OperationContext` carrying both session-level and operation-level fields is simpler. It forces re-resolving the gate registry, charter, and knowledge store on every operation, which is expensive. It also conflates what is stable (policy version, charter) with what accumulates (gate results, evidence IDs), making the schema harder to read. Two tiers match the two lifetimes; the separation is structural, not cosmetic. |

---

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — `ImmutableEvidenceNode` and `EvidenceChain`; every evidence node emitted during a governed operation is sealed with `ctx.to_evidence_data()` as its active assertion payload
- [ADR-0043](ADR-0043-governed-tool-declaration.md) — `ActionManager.execute_governed()` pipeline; Phase 2 creates the `OperationContext`, Phases 4–6 accumulate gate results, Phase 8 embeds the final context snapshot into the evidence node
- [ADR-0044](ADR-0044-tool-gates.md) — Gate registry and `GateResult` semantics; gates evaluated in Phases 4–6 produce the `GateResult` records accumulated in `OperationContext.gate_results`
- [ADR-0047](ADR-0047-agent-charter.md) — Agent Charter; `charter.version` is captured as `charter_version_active` at `OperationContext` creation time
- [ADR-0052](ADR-0052-policy-resolution.md) — Policy resolution bundle; `policy_release_version` is captured as `policy_version_active` at `OperationContext` creation time
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate signs over evidence nodes; the active assertion in each node makes the certificate independently verifiable
- [ADR-0046](ADR-0046-jit-tool-grant.md) — JIT permit tokens consumed during SOFT gate bypass are appended to `OperationContext.permit_tokens_consumed` via `with_permit_token()`
- [ADR-0045](ADR-0045-break-glass-protocol.md) — Break-glass paths run outside `execute_governed()`; they produce their own evidence records, not `OperationContext`-linked ones
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — `EvidenceRef` kinds (`tool_result`, `model_inference`, etc.) used by `ToolExecutionEvidence`
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — `KnowledgeStore` protocol held in `ServiceContext.knowledge_store`
- prior governance research `01_design/013-service-context/ADR-013-service-context.md` — source pattern: two-tier context, active assertion via `to_evidence_data()`, explicit propagation, immutable transforms
