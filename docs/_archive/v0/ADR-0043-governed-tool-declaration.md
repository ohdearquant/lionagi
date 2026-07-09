# ADR-0043: Governed Tool Declaration

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md), [ADR-0050](ADR-0050-operation-context.md)
**Related**: [ADR-0044](ADR-0044-tool-gates.md), [ADR-0051](ADR-0051-tool-registry-allowlists.md), [ADR-0033](ADR-0033-unified-entity-state-model.md), [ADR-0023](ADR-0023-unified-hook-system.md)

---

## Context

lionagi today allows any registered tool to be called without passing through any governance
checkpoint. A caller invokes `branch.operate(instruction=..., tools=...)`, the `ActionManager`
resolves the tool from its `Pile`, and hands arguments directly to `Tool.func_callable`. The only
policy layer that can intervene is `PermissionPolicy` (`lionagi/agent/permissions.py`), which
applies allowlist/denylist/escalate rules keyed by tool name and action string. `PermissionPolicy`
is powerful for what it does, but it is a policy-side construct: it lives in `AgentConfig`, it is
attached by the caller at agent creation time, and the tool itself carries no record of what
constraints should apply to it.

The structural gap is that enforcement metadata is not co-located with the tool. A `read_file` tool
registered in one branch may require a confirmation gate; the same function registered in another
branch has no gate at all. Whether governance applies depends entirely on whether the agent
constructor remembered to attach the right policy — not on anything declared at the tool definition
site. This violates cross-cutting principle #3: **every constraint must be enforced, not just
documented**. A constraint that exists only in an `AgentConfig` dict can be silently absent.

There is also no mandatory execution pipeline. Nothing prevents `Tool.func_callable` from being
called directly — by test code, by a pre-processor, or by a branch that bypasses the normal
`operate()` path. Evidence emission (`DataLogger`) is also optional and caller-driven. The result
is that lionagi in library mode has no single audit point: different call sites emit different
evidence shapes, and some emit none.

### The applicable prior governance research insight

If there is ANY path that bypasses the enforcement pipeline, compliance is broken. The solution
is not more policy configuration — it is a single mandatory pipeline where every phase is
non-optional and the underlying handler is unreachable from outside the pipeline. Governance
metadata must be attached to the handler at definition time, not assembled at invocation time.
Co-location is what makes drift impossible.

Translated to lionagi: the `@governed_tool` decorator plays the role of `@action`, and
`ActionManager.execute_governed()` is the single enforcement point for all governed tool calls —
no governed tool may bypass it. The `Tool` class remains as the schema and callable carrier;
governance metadata is a separate, frozen attachment.

### Why lionagi needs this

Consider an agent writing to a code repository. The `write_file` tool must: confirm that the
target path is inside an allowed workspace (HARD gate), require an explicit justification when the
path is outside a designated source tree (SOFT gate), log a warning when the diff exceeds 500
lines (ADVISORY gate), and emit an evidence node that links the write operation to its operation
context. In the current architecture, all of this must be wired manually in the `AgentConfig`
hooks. If the hook is omitted, the tool runs ungated with no evidence. The first time a new
developer registers `write_file` in a new agent, they must know to also attach four hooks and a
custom `DataLogger` flush. This is not governance; it is documentation that governance is
possible.

With `@governed_tool`, the gate declarations travel with the function definition. Any agent that
registers `write_file` automatically gets all its governance — not because the caller remembered
to configure it, but because the decorator made it unconditional.

---

## Decision

Introduce a `@governed_tool` decorator that co-locates enforcement metadata with the tool
function at definition time, and a mandatory `ActionManager.execute_governed()` pipeline that
enforces a fixed eight-phase sequence with no bypass path.

---

### 1. `GovernedToolMeta` — Frozen Metadata Dataclass

Governance metadata is stored in a frozen `Element` subclass attached to the existing `Tool`
primitive as `Tool.governance_meta`. Frozen because no runtime code should be able to relax a
constraint after the tool is defined.

```python
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

SafetyClass = Literal["standard", "sensitive", "privileged"]
GateLevel = Literal["hard", "soft", "advisory"]


class ToolGateDeclaration(Element):
    """A single tool-level gate declaration resolved by ADR-0044."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    gate_id: str
    enforcement: GateLevel
    justification_prompt: str | None = None


def _gate_pile() -> Pile[ToolGateDeclaration]:
    return Pile(item_type={ToolGateDeclaration})


class GovernedToolMeta(Element):
    """Frozen governance metadata stored on the existing Tool primitive."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    # Evidence chain
    evidence_type: str | None = None
    """Dot-separated category written into every evidence node produced by
    this tool.  If None, defaults to the function name at registration time."""

    skip_evidence: bool = False
    """True only for hot-path read-only probes where evidence is noise
    (e.g. status checks, health pings).  Default False."""

    # Gate declarations — resolved against the registry in ADR-0044
    hard_gates: Pile[ToolGateDeclaration] = Field(default_factory=_gate_pile)
    """Gates whose failure terminates the call immediately.  No justification
    can override a hard-gate failure.  Evaluated before soft gates."""

    soft_gates: Pile[ToolGateDeclaration] = Field(default_factory=_gate_pile)
    """Gates that pause execution and require a documented justification to
    proceed.  May continue after justification is accepted."""

    advisory_gates: Pile[ToolGateDeclaration] = Field(default_factory=_gate_pile)
    """Gates that emit a warning and proceed unconditionally.  Used for
    metrics, audit noise, and developer feedback."""

    # Input schema
    options_schema: type[BaseModel] | None = Field(default=None, exclude=True)
    """Pydantic model for input normalization (Phase 1).  When provided,
    raw kwargs are coerced through this model before reaching the handler."""

    # Classification
    safety_class: SafetyClass = "standard"
    """standard | sensitive | privileged.  Drives which additional class-level
    gates the registry adds per ADR-0044 specificity rules."""

    # Sentinel — set by the decorator, not the caller
    _IS_GOVERNED: ClassVar[bool] = True


# In lionagi/protocols/action/tool.py, extend the existing class in place.
class Tool(Element):
    ...

    governance_meta: GovernedToolMeta | None = Field(
        default=None,
        description="Optional governance metadata declared by @governed_tool.",
    )
```

### 2. `@governed_tool` Decorator

The decorator builds a normal `Tool` instance, attaches `GovernedToolMeta`, and binds governance
phases through `Tool.preprocessor` and `Tool.postprocessor`.

```python
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel

from lionagi.protocols.action.tool import Tool
from lionagi.protocols.generic.pile import Pile


class GovernanceBypassError(RuntimeError):
    """Raised when a governed tool is called directly, bypassing the pipeline."""


def _make_gate_pile(
    gate_ids: Iterable[str] | Pile[ToolGateDeclaration],
    enforcement: GateLevel,
) -> Pile[ToolGateDeclaration]:
    if isinstance(gate_ids, Pile):
        return gate_ids

    gates = Pile(item_type={ToolGateDeclaration})
    for gate_id in gate_ids:
        gates.include(ToolGateDeclaration(gate_id=gate_id, enforcement=enforcement))
    return gates


async def governed_tool_preprocessor(
    kwargs: dict[str, Any],
    *,
    governance_meta: GovernedToolMeta,
    context: "GovernedToolCallContext",
) -> dict[str, Any]:
    """Phases 1-6 run through Tool.preprocessor before func_callable."""
    if governance_meta.options_schema is not None:
        validated = governance_meta.options_schema(**kwargs)
        kwargs = validated.model_dump(exclude_unset=True)

    context.normalized_inputs = dict(kwargs)
    if not governance_meta.skip_evidence and context.operation_context_id is None:
        raise MissingOperationContextError(
            "Governed tools require OperationContext unless skip_evidence=True."
        )

    for gate in await resolve_governed_gates(governance_meta, context):
        result = await run_gate(gate, kwargs, context)
        context.gate_results.include(result)
        if gate.enforcement == "hard" and not result.passed:
            evidence_id = await emit_gate_failure_evidence(gate, result, context)
            raise GateBlockedError(
                gate_id=gate.gate_id,
                tool_name=context.tool_name,
                reason=result.reason,
                evidence_node_id=evidence_id,
            )
        if gate.enforcement == "soft" and not result.passed and not context.justification:
            raise JustificationRequiredError(
                gate_id=gate.gate_id,
                tool_name=context.tool_name,
                prompt=result.justification_prompt or result.reason,
            )

    return kwargs


async def governed_tool_postprocessor(
    result: Any,
    *,
    governance_meta: GovernedToolMeta,
    context: "GovernedToolCallContext",
) -> Any:
    """Phase 8 runs through Tool.postprocessor after func_callable."""
    if not governance_meta.skip_evidence:
        await emit_success_evidence(result, governance_meta, context)
    return result


def governed_tool(
    *,
    evidence_type: str | None = None,
    hard_gates: Iterable[str] | Pile[ToolGateDeclaration] = (),
    soft_gates: Iterable[str] | Pile[ToolGateDeclaration] = (),
    advisory_gates: Iterable[str] | Pile[ToolGateDeclaration] = (),
    skip_evidence: bool = False,
    safety_class: SafetyClass = "standard",
    options_schema: type[BaseModel] | None = None,
) -> Callable[[Callable[..., Any]], Tool]:
    """Decorator that returns the existing Tool primitive with governance_meta.

    Usage::

        @governed_tool(
            evidence_type="repo_write",
            hard_gates=["guard_destructive"],
            soft_gates=["confirm_path_outside_workspace"],
            advisory_gates=["warn_large_diff"],
            safety_class="standard",
            options_schema=WriteFileOptions,
        )
        async def write_file(path: str, content: str) -> WriteResult: ...

    The decorated name is a Tool, not a second GovernedTool hierarchy.
    Governance phases bind through Tool.preprocessor and Tool.postprocessor.
    """

    def decorator(fn: Callable[..., Any]) -> Tool:
        meta = GovernedToolMeta(
            evidence_type=evidence_type or fn.__name__,
            skip_evidence=skip_evidence,
            hard_gates=_make_gate_pile(hard_gates, "hard"),
            soft_gates=_make_gate_pile(soft_gates, "soft"),
            advisory_gates=_make_gate_pile(advisory_gates, "advisory"),
            options_schema=options_schema,
            safety_class=safety_class,
        )
        fn.__governance_meta__ = meta  # type: ignore[attr-defined]
        return Tool(
            func_callable=fn,
            request_options=options_schema,
            governance_meta=meta,
            preprocessor=governed_tool_preprocessor,
            preprocessor_kwargs={"governance_meta": meta},
            postprocessor=governed_tool_postprocessor,
            postprocessor_kwargs={"governance_meta": meta},
        )

    return decorator


def is_governed(tool: Tool | Callable[..., Any]) -> bool:
    """Return True if a Tool or raw callable carries GovernedToolMeta."""
    if isinstance(tool, Tool):
        return tool.governance_meta is not None
    return hasattr(tool, "__governance_meta__")


def get_governed_meta(tool: Tool | Callable[..., Any]) -> GovernedToolMeta:
    """Return the ``GovernedToolMeta`` attached to a governed tool.

    Raises ``TypeError`` if the Tool/callable is not governed.
    """
    meta = tool.governance_meta if isinstance(tool, Tool) else None
    if meta is None:
        meta = getattr(tool, "__governance_meta__", None)
    if meta is None:
        raise TypeError(
            "Tool is not governed. Apply @governed_tool(...) or construct "
            "Tool(governance_meta=..., preprocessor=..., postprocessor=...)."
        )
    return meta
```

### 3. Pipeline Context — Bypass Prevention Mechanism

The pipeline binds per-call state into an invocation-local copy of the `Tool`. The original
`Tool` stays reusable, while `Tool.preprocessor_kwargs` and `Tool.postprocessor_kwargs` carry the
operation context, gate results, justification, Branch reference, and `DataLogger` for that call.

```python
from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field

from lionagi.agent.gates import GateResult
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger
from lionagi.protocols.generic.pile import Pile


def _gate_result_pile() -> Pile[GateResult]:
    return Pile(item_type={GateResult})


class GovernedToolCallContext(Element):
    """Per-call state bound into Tool pre/postprocessor kwargs."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    tool_name: str
    operation_context_id: str | None = None  # links to ADR-0050
    justification: str | None = None
    normalized_inputs: dict[str, Any] = Field(default_factory=dict)
    gate_results: Pile[GateResult] = Field(default_factory=_gate_result_pile)
    branch: Any | None = Field(default=None, exclude=True)
    data_logger: DataLogger | None = Field(default=None, exclude=True)


def bind_governed_context(tool: Tool, context: GovernedToolCallContext) -> Tool:
    """Return an invocation-local Tool copy with pipeline context attached."""
    kwargs = {"governance_meta": tool.governance_meta, "context": context}
    return tool.model_copy(
        update={
            "preprocessor_kwargs": {**tool.preprocessor_kwargs, **kwargs},
            "postprocessor_kwargs": {**tool.postprocessor_kwargs, **kwargs},
        }
    )
```

### 4. `ActionManager.execute_governed()` — The Mandatory Pipeline

Eight phases, all non-optional. Phases 4–6 execute in strict order (hard → soft → advisory).
No phase can be skipped by caller configuration.

```python
from __future__ import annotations

from typing import Any

from lionagi.protocols.action.function_calling import FunctionCalling
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.generic.log import DataLogger


class GateBlockedError(Exception):
    """Raised when a HARD gate blocks execution."""

    def __init__(
        self,
        gate_id: str,
        tool_name: str,
        reason: str,
        evidence_node_id: str | None = None,
    ) -> None:
        self.gate_id = gate_id
        self.tool_name = tool_name
        self.reason = reason
        self.evidence_node_id = evidence_node_id
        super().__init__(f"Hard gate '{gate_id}' blocked '{tool_name}': {reason}")


class JustificationRequiredError(Exception):
    """Raised when a SOFT gate requires justification and none is provided."""

    def __init__(self, gate_id: str, tool_name: str, prompt: str) -> None:
        self.gate_id = gate_id
        self.tool_name = tool_name
        self.prompt = prompt
        super().__init__(
            f"Soft gate '{gate_id}' requires justification for '{tool_name}': {prompt}"
        )


class MissingOperationContextError(Exception):
    """Raised when a governed tool requires ADR-0050 context and none exists."""


class ActionManager:
    """Facade over the Tool Pile; governs all tool executions.

    This extends the existing ActionManager path. Governed tools are still
    ordinary Tool instances; their phases run through Tool.preprocessor,
    FunctionCalling._invoke(), and Tool.postprocessor.
    """

    async def execute_governed(
        self,
        tool: Tool,
        raw_kwargs: dict[str, Any],
        *,
        branch: Any | None = None,
        operation_context_id: str | None = None,
        justification: str | None = None,
        data_logger: DataLogger | None = None,
    ) -> Any:
        """Execute a governed tool through the mandatory eight-phase pipeline.

        Args:
            tool: The registered ``Tool`` with ``governance_meta`` attached.
            raw_kwargs: Raw keyword arguments from the LLM's ``ActionRequest``.
            branch: Calling Branch, supplied for gate hooks that need manager access.
            operation_context_id: ID of the active OperationContext (ADR-0050).
                Required for governed tools; raises if absent and tool is not
                skip_evidence.
            justification: Caller-supplied justification for SOFT gate bypass.
            data_logger: Branch ``DataLogger`` used for evidence emission.

        Returns:
            The handler's return value on success.

        Raises:
            GateBlockedError: If any HARD gate fails.
            JustificationRequiredError: If a SOFT gate fails and no justification
                is supplied.
            MissingOperationContextError: If the tool is governed and
                operation_context_id is None (per ADR-0050).
        """
        meta = get_governed_meta(tool)
        if not meta.skip_evidence and operation_context_id is None:
            raise MissingOperationContextError(
                f"Governed tool '{tool.function}' requires an active OperationContext "
                f"(ADR-0050).  Pass operation_context_id= or set skip_evidence=True."
            )

        context = GovernedToolCallContext(
            tool_name=tool.function,
            operation_context_id=operation_context_id,
            justification=justification,
            branch=branch,
            data_logger=data_logger,
        )
        invocation_tool = bind_governed_context(tool, context)

        # FunctionCalling._invoke() supplies the existing execution lifecycle:
        # Phase 1-6: Tool.preprocessor
        # Phase 7:   Tool.func_callable
        # Phase 8:   Tool.postprocessor
        function_call = FunctionCalling(
            func_tool=invocation_tool,
            arguments=raw_kwargs,
        )
        try:
            await function_call.invoke()
        except Exception as exc:
            await emit_execution_failure_evidence(exc, meta, context)
            raise

        return function_call.execution.response

    async def execute_raw(
        self,
        tool: Tool,
        kwargs: dict[str, Any],
    ) -> Any:
        """Execute an ungoverned tool without the governance pipeline.

        Only available in library mode.  Strict governance mode raises
        ``GovernanceModeError`` from this method at startup configuration.

        Ungoverned tools registered via ``branch.register_tools([plain_function])``
        continue to work through this path with zero configuration changes.
        """
        if is_governed(tool):
            raise GovernanceBypassError(
                f"Governed tool '{tool.function}' must use execute_governed()."
            )

        function_call = FunctionCalling(func_tool=tool, arguments=kwargs)
        await function_call.invoke()
        return function_call.execution.response
```

### 5. Evidence Emission on Success and Failure

Phase 8 emits an evidence node per ADR-0041. The node carries the gate results active at
execution time ("active assertion") so the evidence is self-contained for audit.

```python
from __future__ import annotations

from typing import Any

from lionagi.protocols.generic.log import Log
from lionagi.protocols.governance.evidence import ImmutableEvidenceNode  # ADR-0041


class MissingEvidenceLoggerError(Exception):
    """Raised when evidence must be emitted but Branch DataLogger is absent."""


async def _log_evidence(
    node: ImmutableEvidenceNode,
    context: GovernedToolCallContext,
) -> str:
    if context.data_logger is None:
        raise MissingEvidenceLoggerError(
            f"Governed tool '{context.tool_name}' requires Branch DataLogger."
        )
    await context.data_logger.alog(Log.create(node))
    return str(node.id)


async def emit_success_evidence(
    result: Any,
    meta: GovernedToolMeta,
    context: GovernedToolCallContext,
) -> str:
    """Emit immutable evidence for a successful Tool.postprocessor phase."""
    node = ImmutableEvidenceNode(
        kind="tool_result",  # EvidenceRef kind from ADR-0033
        evidence_type=meta.evidence_type,
        tool_name=context.tool_name,
        operation_context_id=context.operation_context_id,
        inputs_summary=_redact_sensitive(context.normalized_inputs, meta.safety_class),
        result_summary=_redact_sensitive(result, meta.safety_class),
        outcome="success",
        justification=context.justification,
        gate_results=[r.to_dict(mode="json") for r in context.gate_results],
    )
    return await _log_evidence(node, context)


async def emit_gate_failure_evidence(
    gate: ToolGateDeclaration,
    gate_result: Any,
    context: GovernedToolCallContext,
) -> str:
    """Emit immutable evidence for a HARD gate block."""
    node = ImmutableEvidenceNode(
        kind="tool_result",
        evidence_type=context.tool_name,
        tool_name=context.tool_name,
        operation_context_id=context.operation_context_id,
        outcome="gate_blocked",
        gate_id=gate.gate_id,
        gate_reason=gate_result.reason,
    )
    return await _log_evidence(node, context)


async def emit_execution_failure_evidence(
    exc: Exception,
    meta: GovernedToolMeta,
    context: GovernedToolCallContext,
) -> str:
    """Emit immutable evidence when the handler fails after gates pass."""
    node = ImmutableEvidenceNode(
        kind="tool_result",
        evidence_type=meta.evidence_type,
        tool_name=context.tool_name,
        operation_context_id=context.operation_context_id,
        inputs_summary=_redact_sensitive(context.normalized_inputs, meta.safety_class),
        outcome="execution_failed",
        error_type=type(exc).__name__,
        error_message=str(exc),
        gate_results=[r.to_dict(mode="json") for r in context.gate_results],
    )
    return await _log_evidence(node, context)
```

### 6. Bypass Prohibition — Why It Is Enforced at Runtime

`@governed_tool` returns a `Tool`, not a directly callable wrapper. A governed tool that reaches
the legacy raw path raises `GovernanceBypassError`, and a governed `Tool` invoked without the
context bound by `ActionManager.execute_governed()` fails in its `Tool.preprocessor` before the
handler runs. The enforcement mechanism is:

1. `execute_governed()` creates a `GovernedToolCallContext` containing the operation context,
   gate result `Pile`, justification, Branch reference, and `DataLogger`.
2. `bind_governed_context()` copies the existing `Tool` and attaches that context to
   `Tool.preprocessor_kwargs` and `Tool.postprocessor_kwargs`.
3. `FunctionCalling._invoke()` runs the existing lifecycle: preprocessor, handler,
   postprocessor. Missing context means the preprocessor cannot run the governed phases and the
   call fails closed before `Tool.func_callable`.

This means tests must invoke governed tools via `ActionManager.execute_governed()` with mock gate
hooks and a mock `DataLogger`. This is intentional: it forces tests to exercise the pipeline, not
just the handler.

### 7. Backward Compatibility — Library Mode

Ungoverned tools registered via `branch.register_tools([plain_function])` are unaffected.
The `ActionManager` dispatches via `execute_raw()` when `is_governed(tool)` is
`False`. No existing library-mode code changes.

The governance pipeline is opt-in at the tool definition site:

| Tool definition | `is_governed` | Dispatch path |
|---|---|---|
| `def my_tool(...): ...` | False | `execute_raw()` — legacy behavior |
| `@governed_tool(...) async def my_tool(...): ...` | True | `execute_governed()` — mandatory pipeline |

Deployments requiring strict governance may configure `ActionManager` to reject ungoverned tools
at registration time: `execute_raw()` raises `GovernanceModeError` when strict governance mode
is enabled. This is a configuration flag, not a code change — library-mode callers are
unaffected.

---

## Worked Example

A governed `read_file` tool illustrating the full lifecycle.

```python
from pydantic import BaseModel


class ReadFileOptions(BaseModel):
    path: str
    encoding: str = "utf-8"
    max_bytes: int = 1_048_576  # 1 MiB


class ReadFileResult(BaseModel):
    path: str
    content: str
    bytes_read: int


@governed_tool(
    evidence_type="fs.read",
    hard_gates=["guard_paths"],          # block reads outside allowed roots
    soft_gates=["confirm_sensitive_ext"],  # .env, .key, .pem require justification
    advisory_gates=["warn_large_file"],  # warn if > max_bytes threshold
    skip_evidence=False,
    safety_class="standard",
    options_schema=ReadFileOptions,
)
async def read_file(
    path: str,
    encoding: str = "utf-8",
    max_bytes: int = 1_048_576,
) -> ReadFileResult:
    """Read a file from the filesystem."""
    import aiofiles

    async with aiofiles.open(path, encoding=encoding) as fh:
        content = await fh.read(max_bytes)

    return ReadFileResult(path=path, content=content, bytes_read=len(content.encode()))
```

**On successful execution** (path inside allowed root, not a sensitive extension):

- Phase 1: `ReadFileOptions(path=..., encoding="utf-8", max_bytes=1048576)` coerced.
- Phase 2: `operation_context_id` must be set by the calling Branch.
- Phase 3: Gate registry resolves `guard_paths` (hard), `confirm_sensitive_ext` (soft),
  `warn_large_file` (advisory).
- Phase 4: `guard_paths` passes — path is inside `allowed_roots`.
- Phase 5: `confirm_sensitive_ext` passes — file extension is `.py`.
- Phase 6: `warn_large_file` passes — file is 2 KiB.
- Phase 7: Handler runs; returns `ReadFileResult`.
- Phase 8: Evidence node emitted:

  ```json
  {
    "ln_id": "01JXKM...",
    "kind": "tool_result",
    "evidence_type": "fs.read",
    "tool_name": "read_file",
    "operation_context_id": "01JXKL...",
    "outcome": "success",
    "gate_results": {
      "guard_paths": "passed",
      "confirm_sensitive_ext": "passed",
      "warn_large_file": "passed"
    },
    "immutable": true
  }
  ```

**On `guard_paths` failure** (path outside allowed root):

- Phases 1–3: same as above.
- Phase 4: `guard_paths` fails → `GateBlockedError` raised immediately.
- Evidence node emitted with `"outcome": "gate_blocked", "gate_id": "guard_paths"`.
- Phases 5–8 do not execute. The handler is never called.

**On `.env` read without justification**:

- Phase 4: `guard_paths` passes (`.env` is inside allowed root).
- Phase 5: `confirm_sensitive_ext` fails, `justification=None` →
  `JustificationRequiredError` raised with `prompt="Reading .env file requires a
  documented reason."`.
- The caller must re-invoke with `justification="Debugging auth config in dev environment"`.

---

## Consequences

**Positive**

- **Single audit point**: every governed tool call emits evidence. No call site can opt out.
- **Governance visible at definition**: reading the function tells you exactly what gates apply
  and what evidence category is emitted. No separate config file lookup required.
- **Fail-closed universally**: Phase 2 raises on missing Operation Context; Phase 4 raises on
  any hard gate failure; any exception in Phase 7 emits failure evidence. Ambiguity → deny.
  This is cross-cutting principle #1 made structural.
- **Testability**: `GovernanceBypassError` and missing-context preprocessor failures force test
  authors to exercise the pipeline. Integration with mock gate hooks and `DataLogger` is explicit,
  not accidental.
- **Backward compatibility**: ungoverned tools registered via `register_tools()` continue to
  work without any migration. The decorator is an opt-in escalation, not a breaking change.
- **Evidence carries gate results**: the evidence node records which gates ran and their
  outcomes at execution time. This satisfies cross-cutting principle #2 — evidence is not
  just a log entry; it encodes the policy state active at the moment of execution.

**Negative**

- **Decorator boilerplate**: each governed tool needs `@governed_tool(...)` with meaningful
  gate declarations. A tool with no metadata compiles but defeats the purpose.
- **Direct-call friction in tests**: decorated governed tools are `Tool` instances, and governed
  `Tool` instances cannot use the raw path. Test authors must adapt to call through
  `ActionManager.execute_governed()`.
- **Sequential gate execution**: the eight-phase pipeline adds latency proportional to the
  number of gates resolved. For `skip_evidence=True` tools with no gates, the overhead is
  a single preprocessor context check per call.
- **Invocation-local context copy**: `execute_governed()` must bind a fresh context copy for each
  call. Reusing a shared `Tool` without rebinding context fails closed in the preprocessor; this is
  safer than silently sharing gate results across concurrent calls.

---

## Non-Goals

Explicitly out of scope:

- **Gate implementation** (what each gate does, how gate results are computed): this is
  ADR-0044. This ADR only declares the gate slots and the execution order.
- **JIT grants and break-glass overrides**: an agent acquiring a tool grant at runtime is
  ADR-0046. This ADR does not address dynamic permission escalation.
- **Policy resolution** (which gates apply in a given context): this is ADR-0052.
  The `GovernedToolMeta` carries tool-level declarations; policy overlay is resolved before
  Phase 3.
- **Tool registry** (where governed tools are stored, how they are discovered, what allowlists
  look like): this is ADR-0051. This ADR concerns declaration at definition time; the
  registry concern is how those declarations are indexed at runtime.
- **Task Certificate** (the signed proof that all phases completed): this is ADR-0042.
  Evidence nodes emitted in Phase 8 feed the certificate, but certificate construction
  is outside this ADR's scope.
- **Log tier governance** (whether evidence nodes are MUTABLE/PROTECTED/IMMUTABLE): this
  is ADR-0049. Evidence nodes produced in Phase 8 are always IMMUTABLE per ADR-0041;
  the tier assignment protocol is separate.

---

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| **Middleware pattern** (framework-level hook wrapping all tool calls) | Action-specific gates are impossible: middleware sees tool name and args, not tool-level declarations. A `guard_paths` gate for `read_file` and a `guard_destructive` gate for `write_file` cannot be differentiated at the middleware layer without rebuilding the same per-tool metadata structure this ADR provides. Rejected per prior research. |
| **Policy-only enforcement** (extend `PermissionPolicy` with gate declarations) | Enforcement metadata remains at the call site (agent constructor), not the tool definition. Two agents registering the same `write_file` function can have inconsistent policies. Drift is structurally permitted. Violates principle #3. |
| **Separate config files** (`governance.yaml` mapping function names to gate lists) | Config files desync from code: rename a function, forget to update the config, governance silently breaks. Co-location is not optional when the goal is "every constraint enforced." |
| **Multiple decorators** (`@gate_check`, `@emit_evidence`, `@fail_closed` separately) | Ordering between decorators is implicit. A developer can apply `@emit_evidence` without `@gate_check`. There is no single place to read the full governance specification of a tool. Evidence correlation across phases is lost. Rejected per prior research `Decorator-Only Pattern`. |
| **No governed tools** (keep current `PermissionPolicy` as the only layer) | Sufficient for library mode. Insufficient for governed deployments where the audit trail must demonstrate every tool call was gated and every result was evidenced — not merely that a policy was configured. |

---

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — Evidence nodes emitted in Phase 8; append-only store
- [ADR-0044](ADR-0044-tool-gates.md) — Gate registry: what each gate ID resolves to, HARD/SOFT/ADVISORY semantics, specificity rules, class-level gate merging
- [ADR-0050](ADR-0050-operation-context.md) — OperationContext required in Phase 2; links tool call to session and agent identity
- [ADR-0051](ADR-0051-tool-registry-allowlists.md) — Where governed tools register; append-only allowlist entries
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate accumulates evidence nodes from Phase 8
- [ADR-0046](ADR-0046-jit-tool-grant.md) — JIT grants acquired at runtime; interact with SOFT gate resolution
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — `EvidenceRef` kinds (tool_result, model_inference, etc.) used in Phase 8 evidence nodes
- [ADR-0023](ADR-0023-unified-hook-system.md) — Existing hook system; `guard_destructive`, `guard_paths`, `log_tool_use` are candidates for migration to gate declarations
