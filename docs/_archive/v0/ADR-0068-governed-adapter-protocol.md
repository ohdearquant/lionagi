# ADR-0068: Zero-Rewrite Governed Adapter Protocol

Status: accepted
Date: 2026-05-27
Decision owners: @governance-maintainers
Supersedes: none
Superseded by: none
Depends on: ADR-0042, ADR-0044, ADR-0047
Related: ADR-0041, ADR-0043, ADR-0050, ADR-0051, ADR-0052

## Context

Enterprise teams adopting lionagi governance already operate agent stacks built on third-party
frameworks: LangChain chains and agent executors, CrewAI crews, the openai-agents SDK Runner,
and the Anthropic Agent SDK. These systems are in production, have been tested under their own
framework semantics, and carry non-trivial setup cost.

Asking teams to rewrite their agent logic into lionagi `Branch` constructs before governance can
be applied is a non-starter. Rewriting means re-testing the agent under framework semantics,
re-certifying prompts, and accepting a migration risk window where neither the old framework nor
the new one is the sole authoritative execution path. In practice, enterprises either skip
governance entirely or defer migration indefinitely. The correct posture is to meet the agent
where it lives.

The second constraint is overhead. Governance machinery — gate evaluation, evidence recording,
certificate minting — adds latency and memory pressure. When no compliance requirement is active
for a given session, that overhead must be zero. Any always-on wrapper that imports governance
internals at module load time is unacceptable in this position because it forces all callers to
pay for governance even when they do not use it.

The third constraint is import isolation. Third-party frameworks are optional dependencies.
`lionagi` core must be importable on a machine that has never installed LangChain, CrewAI, or
the openai-agents SDK. Adapter modules must guard every framework import so that loading the
adapter module itself does not fail if the target framework is absent.

This ADR formalizes the `GovernedAdapter` base class and the four concrete adapters
(`GovernedChain`, `GovernedCrew`, `GovernedOpenAIAgent`, `GovernedAnthropicAgent`) as the
canonical mechanism for applying lionagi governance to any existing agent framework object
without modifying that object.

## Decision

Introduce `GovernedAdapter`, an abstract async base class in `lionagi/adapters/governed_base.py`,
that wraps any agent framework object and applies lionagi governance through a well-defined
`execute()` lifecycle. The class is the sole entry point for zero-rewrite governance integration.

Governance is opt-in. When `charter=None` (the default), the adapter is a transparent
pass-through: no governance module is imported, no gate is evaluated, `execute()` calls
`_call_wrapped()` and returns `(result, None)`. When a charter is supplied at construction time,
`GovernedFlowController` is instantiated and the full governance lifecycle activates.

Four concrete subclasses ship in `lionagi/adapters/` as optional adapters. Each requires its
framework's package to be installed separately; none is bundled into the core `lionagi` package:

- `langchain.py` — `GovernedChain` for LangChain `Runnable`, `Chain`, `AgentExecutor`
- `crewai.py` — `GovernedCrew` for CrewAI `Crew`
- `openai_agents.py` — `GovernedOpenAIAgent` for openai-agents `Agent` or `Runner`
- `anthropic_agents.py` — `GovernedAnthropicAgent` for Anthropic Agent SDK `Agent`
  (requires the optional `anthropic` package)

All four adapters are peers: the Anthropic adapter is one of several optional integrations, not
a first-class or preferred integration. New frameworks are supported by subclassing
`GovernedAdapter` and implementing two methods: `_call_wrapped()` and optionally
`_get_tool_name()`. The base class handles the full governance lifecycle without any changes.

`GovernanceViolationError` is defined as a standalone class in `governed_base.py`. It does not
import from `lionagi.protocols.governance`. When the full governance module is available at
runtime, the canonical error class from `lionagi.protocols.governance` takes precedence. When it
is absent, the local stub is sufficient. This design allows callers to import and catch
`GovernanceViolationError` from the adapter module without requiring the governance stack.

## Scope

This ADR owns:

- `GovernedAdapter` base class and its `execute()` lifecycle contract
- `GovernanceViolationError` local definition and import-time replacement semantics
- `_call_wrapped()`, `_get_tool_name()`, `_hash_args()`, `_hash_result()` adapter contract
- `on_deny` policy enumeration and behavior (`"raise"`, `"skip"`, `"log"`)
- return type contract `(result, TaskCertificate | None)`
- lazy framework import pattern for concrete adapters
- four bundled concrete adapters: `GovernedChain`, `GovernedCrew`, `GovernedOpenAIAgent`,
  `GovernedAnthropicAgent`

This ADR consumes, but does not own:

- `TaskCertificate` from ADR-0042 (Task Certificate)
- `GateResult` and `GateVerdict` from ADR-0044 (Tool Gates)
- `AgentCharter` and charter activation from ADR-0047 (Agent Charter)
- `GovernedFlowController` from the governance flow integration module

## Non-Goals

- No framework-specific governance rules. All gate logic lives in the charter and ADR-0044
  `ToolGate` definitions, not in the adapter subclasses.
- No automatic framework detection. The caller explicitly chooses the adapter class for their
  framework object.
- No request or response transformation. The adapter forwards arguments verbatim to the wrapped
  object and returns its result unchanged.
- No tenant isolation. The adapter applies the charter supplied at construction time. Multi-tenant
  charter routing is not included in the adapter protocol.
- No synchronous public API. `execute()` and all concrete `run()` methods are `async`. Sync
  callers must use `asyncio.run()` or an equivalent executor bridge.
- No framework version pinning. Adapters support whatever version of the framework is installed
  by probing method existence (`hasattr`) rather than version checks.

## Interfaces And Types

### GovernedAdapter Constructor

```python
class GovernedAdapter:
    def __init__(
        self,
        wrapped: Any,
        charter: Any = None,
        session_id: str = "",
        on_deny: str = "raise",
    ) -> None: ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `wrapped` | `Any` | required | The framework object to wrap (Chain, Crew, Agent, Runner). |
| `charter` | `Any` | `None` | Charter path, YAML string, or `CharterDocument`. `None` disables governance entirely. |
| `session_id` | `str` | `""` | Caller-supplied session identifier embedded in the certificate. |
| `on_deny` | `str` | `"raise"` | Policy applied on hard gate denial. One of `"raise"`, `"skip"`, `"log"`. |

Raises `ValueError` if `on_deny` is not one of the three accepted values.

Raises `ImportError` if `charter` is not `None` and
`lionagi.protocols.governance.flow_integration` cannot be imported.

### execute() Public Contract

```python
async def execute(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
    """Execute the wrapped object under governance.

    Returns (result, TaskCertificate | None).
    Certificate is None when no charter is active.
    """
```

`execute()` is the primary entry point. Subclasses call `super().execute()` from their
framework-specific `run()` method, forwarding all arguments unchanged.

### Adapter Contract Methods

```python
async def _call_wrapped(self, *args: Any, **kwargs: Any) -> Any:
    """Invoke the wrapped framework object. MUST be overridden."""
    raise NotImplementedError(...)

def _get_tool_name(self) -> str:
    """Return the gate registration lookup key for this adapter.

    Default: governed.{type(self._wrapped).__name__.lower()}
    Concrete adapters override this to return stable, human-readable names.
    """

def _hash_args(self, args: tuple, kwargs: dict) -> str:
    """Return SHA-256 hex digest of the serialized args/kwargs."""

def _hash_result(self, result: Any) -> str:
    """Return SHA-256 hex digest of the serialized result."""
```

Subclasses MUST override `_call_wrapped()`. Overriding `_get_tool_name()` is strongly
recommended to produce stable, readable tool names for gate configuration. `_hash_args()` and
`_hash_result()` may be overridden to provide framework-specific serialization if the default
JSON serialization does not produce stable digests for the framework's output types.

### GovernanceViolationError

```python
class GovernanceViolationError(Exception):
    def __init__(self, message: str = "Governance gate denied the operation") -> None: ...
```

Raised by `execute()` when `on_deny="raise"` and a gate returns `GateVerdict.DENY`. Callers
that catch this error are guaranteed to receive at minimum the local definition. When the
governance module is available, the raised instance is compatible with
`lionagi.protocols.governance.GovernanceViolationError`.

### on_deny Policies

| Policy | Behavior on Gate DENY |
|--------|----------------------|
| `"raise"` | Raises `GovernanceViolationError` with the gate ID and justification. Execution is blocked. The `(result, cert)` tuple is never returned. |
| `"skip"` | Returns `(None, None)` immediately without executing the wrapped object. No exception is raised. The caller must handle `None` result. |
| `"log"` | Emits a `warnings.warn()` at the call site, then returns `(None, None)`. Execution is skipped even though no exception is raised. Useful in development or observability-only deployments where blocking would be too disruptive. |

All three policies apply only to hard `GateVerdict.DENY` outcomes. Soft-gate justification
flows (ADR-0044) remain controlled by the charter and are not exposed through `on_deny`.

### Supported Framework Adapters

| Framework | Adapter Class | Import Path | Tool Name | Native Async | Sync Fallback | Optional Dependency |
|-----------|--------------|-------------|-----------|:---:|:---:|---|
| LangChain | `GovernedChain` | `lionagi.adapters.langchain` | `langchain.chain` | Yes (`ainvoke`) | `invoke` via executor | `langchain` |
| CrewAI | `GovernedCrew` | `lionagi.adapters.crewai` | `crewai.crew` | Future (`akickoff`) | `kickoff` via executor | `crewai` |
| openai-agents | `GovernedOpenAIAgent` | `lionagi.adapters.openai_agents` | `openai_agents.run` | Yes (`Runner.run`) | N/A | `openai-agents` |
| Anthropic Agent SDK | `GovernedAnthropicAgent` | `lionagi.adapters.anthropic_agents` | `anthropic_agents.run` | Yes (`arun`) | `run` via executor | `anthropic` |

All four adapters are optional. The `lionagi` core package does not require any of these
framework dependencies. Install only the packages for the frameworks you use.

LangChain async preference order: `ainvoke` → `invoke` (executor) → `arun` (legacy).
CrewAI async preference order: `akickoff` → `kickoff` (executor).
Anthropic SDK async preference order: `arun` → `run` (executor).

All four adapters raise `AttributeError` with a clear message if the wrapped object exposes
none of the expected interface methods.

## Runtime Semantics

The `execute()` lifecycle runs five phases in order:

```text
1. pre_op_check     — gate evaluation before the wrapped object runs
2. _call_wrapped    — the framework object executes
3. elapsed timing   — wall-clock milliseconds recorded
4. post_op_record   — evidence recorded after execution
5. mint_certificate — TaskCertificate or None returned to caller
```

### Phase 1: pre_op_check

When `self._controller` is `None` (no charter), this phase returns `None` immediately. No gate
is evaluated.

When a controller is active, `GovernedFlowController.pre_op_check(tool_name)` is called with
the string returned by `_get_tool_name()`. The result is a `GateResult` (ADR-0044). If the
verdict is `GateVerdict.DENY`, the `on_deny` policy is applied and `execute()` returns without
proceeding to phase 2.

### Phase 2: _call_wrapped

The subclass implementation runs the framework object. All positional and keyword arguments
passed to `execute()` are forwarded without modification. The result is any Python value
returned by the framework object.

If the framework raises an exception, the exception propagates through `execute()` uncaught.
The post-op record phase does not run and no certificate is minted. This is intentional:
failed executions do not produce certificates in the adapter protocol. Callers that need
failure certificates should use the `Branch`-level governance path (ADR-0043).

### Phase 3: Elapsed Timing

Wall-clock elapsed time is measured using `time.perf_counter()`. The value is passed to
`post_op_record` as `elapsed_ms` and recorded in the evidence chain when a controller is
active.

### Phase 4: post_op_record

When `self._controller` is `None`, this phase is a no-op.

When a controller is active, `args_hash` (SHA-256 of serialized arguments) and `result_hash`
(SHA-256 of serialized result) are passed to `GovernedFlowController.post_op_record()` along
with the gate result from phase 1 and the elapsed time. These hashes form the evidence entry
for the operation. Raw argument values and result values are never stored — only their digests.
This prevents PII leakage when argument content includes user data.

If phase 1 returned `None` (no charter but a controller exists through future subclass
override), `post_op_record` synthesizes a passthrough `GateResult` with `verdict=ALLOW` before
recording.

### Phase 5: mint_certificate

When `self._controller` is `None`, this phase returns `None`.

When a controller is active, `GovernedFlowController.mint_certificate()` is called. It
assembles the `TaskCertificate` (ADR-0042) from the accumulated evidence and gate results.
Certificate grade is determined by gate outcomes: any soft denial that was overridden or any
advisory denial recorded during the session degrades the certificate below the top grade. Full
grade semantics are owned by ADR-0042.

## Evidence And Trace Requirements

When a charter is active, every `execute()` call that completes without exception must produce:

- one `args_hash` and one `result_hash` (SHA-256 hex, 64 characters each)
- one `GateResult` (ADR-0044) from pre-gate evaluation, or a synthesized ALLOW result
- one evidence record via `GovernedFlowController.post_op_record()` with `tool_name`,
  `args_hash`, `result_hash`, `gate_result`, and `elapsed_ms`
- one `TaskCertificate` (ADR-0042) from `GovernedFlowController.mint_certificate()`

The adapter does not produce span or trace records directly. Tracing is the responsibility of
the `GovernedFlowController` and the governance flow integration layer. Adapters that wrap
frameworks with their own tracing (LangChain callbacks, CrewAI telemetry) do not interfere
with those frameworks' traces.

## Test Requirements

All concrete adapter implementations must include tests that verify:

- Without charter: `execute()` returns `(result, None)` and no governance module is imported.
- With charter: `execute()` returns `(result, TaskCertificate)` and evidence is recorded.
- `on_deny="raise"`: gate DENY raises `GovernanceViolationError`.
- `on_deny="skip"`: gate DENY returns `(None, None)` without exception.
- `on_deny="log"`: gate DENY emits a `warnings.warn()` and returns `(None, None)`.
- Invalid `on_deny` value raises `ValueError` at construction time.
- Framework import error raises `ImportError` with installation instructions.
- `_call_wrapped()` not overridden raises `NotImplementedError`.
- `args_hash` and `result_hash` are 64-character hex strings.
- Framework exception in `_call_wrapped()` propagates unchanged.

Coverage target: ≥90% of `governed_base.py` lines.

## Consequences

**Positive**

- Enterprise teams apply governance to existing agent stacks without any rewrite.
- Zero overhead when no charter is supplied — no imports, no gate evaluation, no evidence
  recording. The pass-through path has no measurable latency impact.
- New frameworks require only a `_call_wrapped()` implementation; the full governance lifecycle
  is inherited without modification.
- `GovernanceViolationError` can be caught without importing the governance stack, enabling
  lightweight error handling in environments where the governance module is optional.
- Hash-based evidence avoids PII leakage — raw user inputs and agent outputs are never
  persisted.
- Lazy framework imports prevent dependency pollution: importing
  `lionagi.adapters.langchain` on a machine without LangChain does not fail until `run()` is
  actually called.

**Negative**

- Sync framework calls (CrewAI `kickoff`, Anthropic SDK `run`) are dispatched through
  `asyncio.get_event_loop().run_in_executor()`, which incurs thread-pool overhead and requires
  an active event loop.
- Framework exceptions during `_call_wrapped()` bypass evidence recording and certificate
  minting entirely. Callers that need audit trails for failures must handle the exception and
  record it separately.
- The adapter's `session_id` is fixed at construction. Sessions with dynamic IDs must
  construct a new adapter instance per session.
- Certificate grade is computed by `GovernedFlowController` based on a single execution.
  Multi-step agents that internally make many tool calls will produce a single adapter-level
  certificate, not per-step certificates. Per-step governance requires using the
  `Branch`-level instrument (ADR-0043).

## Migration

No migration is required for callers that do not use governance. The adapter classes are
additive. Existing uses of LangChain, CrewAI, or openai-agents without lionagi governance are
unaffected.

Callers adding governance for the first time:

1. Replace the direct call to the framework object with an adapter construction:

   ```python
   # Before
   result = await chain.ainvoke(input)

   # After
   from lionagi.adapters.langchain import GovernedChain
   adapter = GovernedChain(chain, charter="policy.yaml", session_id=session_id)
   result, cert = await adapter.run(input)
   ```

2. Install `lionagi>=0.27.0` which includes `lionagi.protocols.governance`.
3. Author a charter document following docs/governance/charter-dsl-v0.md.
4. Handle `GovernanceViolationError` where appropriate in application code.

## Examples

### LangChain Chain Without Governance (Pass-Through)

```python
from langchain.chains import LLMChain
from lionagi.adapters.langchain import GovernedChain

chain = LLMChain(llm=my_llm, prompt=my_prompt)
# No charter supplied: pure pass-through, zero overhead
adapter = GovernedChain(chain)
result, cert = await adapter.run({"question": "What is 2+2?"})
# cert is None
assert cert is None
```

### LangChain Chain With Governance

```python
from langchain_core.runnables import RunnableSequence
from lionagi.adapters.langchain import GovernedChain
from lionagi.adapters.governed_base import GovernanceViolationError

chain = RunnableSequence(prompt | llm | output_parser)
adapter = GovernedChain(
    chain,
    charter="charters/research_policy.yaml",
    session_id="session-abc123",
    on_deny="raise",
)

try:
    result, cert = await adapter.run({"topic": "quantum computing"})
    # cert is a TaskCertificate (ADR-0042)
    print(f"Grade: {cert.grade}, Chain head: {cert.evidence_chain_head}")
except GovernanceViolationError as exc:
    # A gate denied this operation
    print(f"Governance denied: {exc}")
```

### LangChain AgentExecutor With on_deny="skip"

```python
from langchain.agents import AgentExecutor
from lionagi.adapters.langchain import GovernedChain

executor = AgentExecutor(agent=agent, tools=tools)
# "skip" returns (None, None) on denial — no exception
adapter = GovernedChain(executor, charter="policy.yaml", on_deny="skip")
result, cert = await adapter.run("Summarise the quarterly report")
if result is None:
    print("Governance denied; skipping this operation")
```

### CrewAI Crew Without Governance

```python
from crewai import Crew, Agent, Task
from lionagi.adapters.crewai import GovernedCrew

crew = Crew(agents=[researcher, writer], tasks=[research_task, write_task])
# No charter: pass-through
adapter = GovernedCrew(crew)
result, cert = await adapter.run(inputs={"topic": "AI governance"})
assert cert is None
```

### CrewAI Crew With Governance

```python
from crewai import Crew
from lionagi.adapters.crewai import GovernedCrew

crew = Crew(agents=[...], tasks=[...])
adapter = GovernedCrew(
    crew,
    charter="charters/agent_policy.yaml",
    session_id="crew-run-42",
    on_deny="log",
)
result, cert = await adapter.run(inputs={"topic": "competitor analysis"})
# cert is None if governance denied with on_deny="log"
# cert is a TaskCertificate if execution completed
```

### openai-agents SDK With Governance

```python
from agents import Agent  # openai-agents
from lionagi.adapters.openai_agents import GovernedOpenAIAgent

agent = Agent(
    name="research-assistant",
    instructions="You are a research assistant.",
)
adapter = GovernedOpenAIAgent(
    agent,
    charter="policy.yaml",
    session_id="oai-session-99",
    on_deny="raise",
)
result, cert = await adapter.run("List the top 5 recent ML papers.")
print(result.final_output)
print(cert.grade)
```

The adapter accepts either an `agents.Agent` or an `agents.Runner` instance as `wrapped`. When
an `Agent` is passed, `Runner.run(agent, input)` is called implicitly.

### Anthropic Agent SDK With Governance

```python
from anthropic.agents import Agent  # Anthropic Agent SDK
from lionagi.adapters.anthropic_agents import GovernedAnthropicAgent

agent = Agent(
    model="<your-model>",   # e.g. the Anthropic model ID of your choice
    system_prompt="You are a document analyst.",
    tools=[...],
)
adapter = GovernedAnthropicAgent(
    agent,
    charter="charters/doc_policy.yaml",
    session_id="anthropic-session-7",
)
result, cert = await adapter.run("Draft a summary of the attached contract.")
```

### Custom Framework Adapter

Teams using frameworks not covered by the four bundled adapters can implement their own:

```python
from lionagi.adapters.governed_base import GovernedAdapter


class GovernedMyFrameworkAgent(GovernedAdapter):
    """Governed wrapper for MyFramework agent objects."""

    def _get_tool_name(self) -> str:
        return "myframework.agent"

    async def run(self, prompt: str, **kwargs) -> tuple:
        return await self.execute(prompt, **kwargs)

    async def _call_wrapped(self, prompt: str, **kwargs):
        agent = self._wrapped
        # Prefer async interface; fall back to sync via executor
        if hasattr(agent, "arun"):
            return await agent.arun(prompt, **kwargs)
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: agent.run(prompt, **kwargs))
```

The custom adapter inherits the full governance lifecycle, evidence recording, certificate
minting, and all `on_deny` policies without any additional implementation.

### Catching GovernanceViolationError Without Governance Stack

```python
# This import works even if lionagi.protocols.governance is not installed
from lionagi.adapters.governed_base import GovernanceViolationError

try:
    result, cert = await adapter.run(input_data)
except GovernanceViolationError as exc:
    # Handle governance denial
    log_denial(str(exc))
```

## Security Considerations

### Hash-Based Evidence

Arguments and results are recorded as SHA-256 digests, not raw values. This means:

- User-supplied inputs (including potentially sensitive queries, document content, or PII) are
  never written to the evidence chain.
- Framework outputs (which may include proprietary data, summarized customer records, or
  confidential business information) are similarly protected.
- The digest is sufficient for audit purposes: it proves that a specific invocation occurred
  and its inputs and outputs were in a specific state at execution time, without exposing
  the content.

Teams requiring full content logging for compliance must implement that separately in the
framework's native callback or telemetry system, not through the adapter evidence chain.

### Lazy Imports and Dependency Isolation

Each concrete adapter delays its framework import until `_call_wrapped()` is first executed.
This has two security benefits:

1. A misconfigured or compromised version of an optional framework cannot execute code at
   module import time by virtue of being installed.
2. Dependency trees for optional frameworks do not affect the security surface of
   `lionagi.adapters.governed_base` or any unrelated adapter.

### GovernanceViolationError Import Independence

`GovernanceViolationError` is defined without importing from `lionagi.protocols.governance`.
This eliminates a potential circular import chain through the governance stack and ensures that
the error class is always resolvable, even in minimal deployments that install only the adapter
module.

### Charter Supply

The adapter activates governance only when the caller explicitly supplies a `charter`. There is
no ambient or implicit charter. This is deliberate: governance cannot be silently applied by a
dependency or environment variable without the caller's explicit construction-time decision.
Teams that want environment-driven charter activation should construct the adapter with a
charter resolved from their configuration layer, not rely on adapter-level ambient behavior.

### Tenant Isolation

Tenant isolation — routing different callers to different charters based on identity,
namespace, or organizational membership — is not included in this protocol. The adapter applies
exactly the charter supplied at construction time to every execution.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Require callers to rewrite agents as lionagi `Branch` instances | Non-starter for enterprise teams with production agent stacks. Migration risk outweighs governance benefit. |
| Always-on governance wrapper with ambient charter loading | Adds latency and import overhead for every execution even when no charter is active. Violates the zero-overhead requirement for pass-through mode. |
| Monkey-patch framework objects in-place | Brittle — depends on stable internal framework method names. Untestable without framework installation. Rejected in favor of explicit wrapper construction. |
| Protocol / interface-only definition with no bundled adapters | Leaves teams to implement adapter mechanics independently. Inconsistent evidence recording and error handling across implementations. |
| Sync-only adapter API | Many calling contexts are already async. Forcing sync with executor bridges would be less efficient than native async. |
| Store raw args/results in evidence chain | Leaks PII. Hash-based evidence is sufficient for audit purposes and avoids compliance risk. |

## Cross-References

- ADR-0041 (Immutable Evidence Nodes) — `ImmutableEvidenceNode` and `EvidenceChain` are the
  storage substrate for the evidence records written by `post_op_record`. See
  `docs/adrs/ADR-0041-immutable-evidence-nodes.md`.
- ADR-0042 (Task Certificate) — `TaskCertificate`, `CertificateState`, and `Defensibility`
  are the types returned by `mint_certificate()` when a charter is active. See
  `docs/adrs/ADR-0042-task-certificate.md`.
- ADR-0043 (Governed Tool Declaration) — defines the `Branch`-level governance instrument for
  lionagi-native tool calls. The adapter protocol is complementary: it governs whole framework
  objects, not individual tool calls inside a `Branch`. See
  `docs/adrs/ADR-0043-governed-tool-declaration.md`.
- ADR-0044 (Tool Gates) — `GateResult`, `GateVerdict`, and `GateEnforcement` are the canonical
  gate result types consumed by `_pre_op_check()` and `_post_op_record()`. `ToolGate`
  definitions in the charter determine whether a given `tool_name` is allowed. See
  `docs/adrs/ADR-0044-tool-gates.md`.
- ADR-0047 (Agent Charter) — `AgentCharter` and `CharterDocument` are the charter types
  accepted by the `GovernedAdapter` constructor. Charter compilation and activation semantics
  are owned by ADR-0047. See `docs/adrs/ADR-0047-agent-charter.md`.
- ADR-0050 (Operation Context) — `OperationContext` propagation within `GovernedFlowController`
  binds actor identity and policy release to each execution. The adapter does not directly
  construct `OperationContext`; that is the controller's responsibility.
- ADR-0051 (Tool Registry Allowlists) — gate resolution consults the registry for exact tool
  name bindings. The `tool_name` returned by `_get_tool_name()` is the lookup key.
- ADR-0052 (Policy Resolution) — the policy release and bundle in effect during execution are
  resolved by the controller, not the adapter. ADR-0052 owns that resolution algorithm.
- `lionagi/adapters/governed_base.py` — implementation of `GovernedAdapter` and
  `GovernanceViolationError`.
- `lionagi/adapters/langchain.py` — `GovernedChain` implementation.
- `lionagi/adapters/crewai.py` — `GovernedCrew` implementation.
- `lionagi/adapters/openai_agents.py` — `GovernedOpenAIAgent` implementation.
- `lionagi/adapters/anthropic_agents.py` — `GovernedAnthropicAgent` implementation.
