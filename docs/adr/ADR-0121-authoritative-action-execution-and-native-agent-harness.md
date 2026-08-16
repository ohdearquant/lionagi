# ADR-0121: Authoritative action execution and native agent harness

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: actions-tools
- **Date**: 2026-08-16
- **Relations**: extends ADR-0012 (action lifecycle), ADR-0044 (tool controls),
  ADR-0086 (local-tool controls), ADR-0090 (sandbox backend), and ADR-0091
  (per-worker isolation); reconciles and partially supersedes ADR-0087 (evidence-backed governed
  execution) as specified in the authority-reconciliation table below, with D10 retaining the
  outcome/evidence behavior; depends on ADR-0119 (deterministic declaration substrate) and
  ADR-0120 (interception and observation planes); coordinates with ADR-0123 (canonical Run)

## Context

LionAGI currently has the ingredients of an execution system without one authoritative execution
boundary. The result is a set of individually reasonable seams that do not compose into a security
contract.

`protocols.generic.Processor` owns queue workers, capacity, event lifecycle, and a boolean
`request_permission(**event.request)` seam. `RateLimitedAPIExecutor` uses that boolean for quota
admission. `FunctionCalling` invokes a Python callable directly. `ActionManager.invoke()` creates a
`FunctionCalling` and calls it directly. Session authorization is applied only by the `_act`
operation, while Agent factory paths attach tool preprocessors one registration at a time. A caller
that holds an `ActionManager`, a fresh plugin Tool, or a callable can therefore select a path that
does not pass every intended control.

Native coding-agent providers introduce a different boundary. Claude Code, Codex, Gemini, and
similar subprocesses execute many tools inside the provider process. LionAGI sees streamed tool
records after the provider has decided and often after the action has occurred. Those actions
cannot honestly be described as having passed LionAGI's Python `PermissionPolicy`.

The existing `AgentSpec.yolo` flag makes the mismatch worse: one boolean maps to provider-specific
settings with materially different meanings, including approval bypass, workspace write access,
or a provider's own “yolo” mode. A worktree or subprocess is also not a security sandbox.
ADR-0090 already records that its local backend is an execution target for measurement and
isolation, not an OS security boundary.

The desired outcome is not one universal Executor base class. State transactions, flow scheduling,
rate-limit admission, and callable invocation have different invariants. The desired outcome is one
authority for each security-relevant invocation, a provider-neutral harness contract that reports
what a provider can actually enforce, and evidence tied to one explicit execution scope and, when
durable Run semantics are enabled, one canonical Run.

### Current failures and drift

**P1 — callable invocation is bypassable.** The governed `_act` path is not the only public route
to `ActionManager.invoke()` or `FunctionCalling.invoke()`. Hook attachment is construction-path
dependent. Plugin materialization can create a fresh Tool after policy hooks were attached.

**P2 — one boolean conflates admission, authorization, and deferral.** Processor permission is
used for worker capacity and rate limiting. A temporary quota condition is neither a security
denial nor user approval. Boolean results cannot express defer, escalation, provenance, or an
expiry.

**P3 — policy is mutable and incompletely serialized.** `PermissionPolicy` is a mutable dataclass.
AgentSpec YAML omits permissions, while public examples show a mapping even though Agent factory
silently ignores values that are not already `PermissionPolicy`. A reconstructed agent can
therefore have a different effective policy from the declared agent.

**P4 — transformation can invalidate an earlier decision.** Existing preprocessors may rewrite
arguments. A path may authorize the original request but invoke the transformed request without
schema normalization and a second security evaluation.

**P5 — denial and failure have inconsistent shapes.** Session gates, hook decisions, preprocessors,
and callable failures return or raise different values. Some `on_error` hook fields are stored but
never invoked. This makes callers infer whether a tool ran from exceptions and message contents.

**P6 — provider-native execution is observational.** Provider stream `tool_use` and `tool_result`
records prove only what the adapter observed. They do not prove that LionAGI admitted the action,
that an OS sandbox enforced a filesystem boundary, or that an approval prompt was shown.

**P7 — harness configuration has no capability negotiation.** Unsupported provider settings can
be ignored, approximated, or mapped to a stronger setting without a typed report. Required
controls must fail closed; optional controls may degrade only when the caller can inspect and
persist that fact.

**P8 — the shipped subagent permission vocabulary is false.** `SubagentRequest.permissions`
advertises `"inherit"`, then forwards it into `AgentSpec.compose()`, whose resolver accepts only
`safe`, `read_only`, `allow_all`, and `deny_all`. True inheritance needs an unresolved sentinel and
an explicit resolution boundary, not a string that currently raises.

| Concern | Decision |
|---|---|
| Invocation authority | D1: `ActionExecutor` is the only LionAGI-owned callable and MCP invocation path. |
| Manager responsibility | D2: `ActionManager` registers and resolves actions; it never executes them. |
| Request and decisions | D3: immutable `ExecutionRequest` and typed `ActionAdmissionDecision` replace boolean permission seams. |
| Enforcement order | D4: one compiled plan runs validation, authorization, transformation, revalidation, invocation, and evidence exactly once. |
| Policies | D5: declared policy is immutable, serializable, resolved before execution, and attached to the execution scope and optional Run. |
| Native harness | D6: `HarnessSpec` is provider-neutral; adapters compile it and return a `CapabilityReport`. |
| Security planes | D7: harness admission, provider-native controls, Action admission, and local effect containment remain distinct. |
| Capability negotiation | D8: requested provider controls are enforced, degraded, or unsupported and required gaps fail closed. |
| Existing executors | D9: generic Processor is narrowed to event driving; flow and state compose shared mechanics instead of inheriting action semantics. |
| Evidence and compatibility | D10: outcomes preserve requested/decided/enforced/observed/invoked truth, and legacy paths delegate before deletion. |

This ADR is a design gate. Implementation of the authoritative path and harness begins only after
review and acceptance. Narrow fixes that remove a provably invalid public option or redact secrets
may land independently when they do not preselect the larger design.

### Authority reconciliation with ADR-0087

ADR-0087 already specifies stronger evidence semantics than an ordinary executor outcome. This
record does not replace them with a StateDB log. The clause boundary is normative:

| ADR-0087 clause | Result under ADR-0121 |
|---|---|
| D1 invocation controller | **Superseded:** universal `ActionExecutor`, not an optional manager-installed controller, owns invocation. Its private adapter reuses the one-use-token/no-bypass invariant. |
| D2 evidence records/hash | **Retained:** ADR-0087 owns governed evidence record types, redaction, `sha256-v1`, and chain canonicalization until separately amended. |
| D3 evidence store | **Retained:** append-only `EvidenceStore` is authoritative for governed evidence. A StateStore backend may implement that protocol but cannot weaken, replace, update, or delete its records. |
| D4 gate result | **Coordinated:** `GateResult` is an immutable policy-evaluation fact; `ActionAdmissionDecision` is per-action executor control flow. One deterministic reducer connects them without evaluating a control twice. |
| D5 context/policy | **Coordinated:** ADR-0121 owns declaration, inheritance, serialization, and compilation; ADR-0087 owns activated immutable snapshots, exact version/digest pins, history, and `PolicySnapshotStore`. There is no second resolved-policy store or digest. |
| D6 certificate | **Retained:** only `CertificateIssuer` mints a process-only `TaskCertificate` from a closed, verified chain. ActionExecutor does not mint per-action certificates. |
| D7 projection | **Retained:** observations contain post-append references only and remain non-authoritative. |
| D8 later controls | **Still deferred:** `ESCALATE` creates an approval request; it does not silently activate exception grants or break-glass. |

Four profiles prevent an optional persistence feature from becoming a false security claim:

- **minimal SDK:** ActionExecutor plus an ephemeral execution scope; no Run, durable evidence, or
  certificate claim;
- **durable:** a canonical `run_id` plus StateStore/outbox attempt and outcome projections; it may
  still be ungoverned;
- **governed:** the exact ADR-0087 `PolicySnapshotStore` and append-only `EvidenceStore`; every
  required pre-call append is part of control flow;
- **certifiable:** governed execution plus an explicit closed task boundary and verified expected
  evidence-chain head.

## Decision

### D1 — `ActionExecutor` owns every LionAGI action invocation

An **Action** is a callable, built-in tool, plugin tool, or MCP operation that LionAGI can invoke
itself. All Action invocation goes through one injected `ActionExecutor` port. The rule applies to
Session operations, direct SDK use, agents, flows, plugins, CLI, Studio, and MCP composition roots.

```python
class ActionExecutor(Protocol):
    async def execute(
        self,
        request: ExecutionRequest,
        *,
        context: ExecutionContext,
    ) -> ExecutionOutcome: ...
```

No public object exposes an alternate method that calls the underlying Python callable or MCP
transport. Low-level invocation remains private to the executor adapter. Tests may use a test
adapter through the same port.

`FunctionCalling` becomes an internal attempt record/state machine owned by ActionExecutor, or a
compatibility façade that delegates to it. It is not an independently invokable authority.

### D2 — `ActionManager` is a registry and resolver

`ActionManager` owns:

- canonical action identity and aliases;
- schema/spec lookup;
- version and source provenance;
- registration collision rules;
- resolution of one declared reference to an `ActionDescriptor`.

It does not own permission, retries, hooks, message creation, or invocation. Its compatibility
`invoke()` method, while supported, must construct an `ExecutionRequest` and delegate to the
injected ActionExecutor. If no executor is available it raises one typed configuration error; it
must never fall back to a direct callable.

Plugin discovery returns descriptors or factories. Materializing a fresh Tool cannot bypass
policy because policy is compiled into the executor plan at execution time, not attached as a
mutable hook during one construction path.

### D3 — requests and decisions are immutable typed values

The authoritative request uses the deterministic declaration substrate from ADR-0119:

```python
class ExecutionScopeKind(str, Enum):
    EPHEMERAL_OPERATION = "ephemeral_operation"
    DURABLE_RUN = "durable_run"

@dataclass(frozen=True, slots=True, init=False)
class EphemeralOperationScopeRef(Params):
    kind: Literal[ExecutionScopeKind.EPHEMERAL_OPERATION]
    scope_id: IDType

@dataclass(frozen=True, slots=True, init=False)
class DurableRunScopeRef(Params):
    kind: Literal[ExecutionScopeKind.DURABLE_RUN]
    run_id: IDType
    owner_attempt_id: IDType | UnsetType = Unset

ExecutionScopeRef = EphemeralOperationScopeRef | DurableRunScopeRef

@dataclass(frozen=True, slots=True, init=False)
class ExecutionRequest(Params):
    request_id: IDType
    action: ActionRef
    arguments: Mapping[str, JsonValue]
    principal_claim: PrincipalRef | UnsetType
    execution_scope: ExecutionScopeRef
    session_id: IDType | UnsetType = Unset
    deadline: datetime | UnsetType = Unset
    idempotency_key: str | UnsetType = Unset
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

@dataclass(frozen=True, slots=True, init=False)
class ExecutionContext(Params):
    context_id: IDType
    execution_scope: ExecutionScopeRef
    authenticated_principal: PrincipalRef
    tenant: TenantRef | UnsetType = Unset
    policy_snapshot: PolicySnapshotRef | UnsetType = Unset
    effect_backend: ActionBackendRef
    authentication_evidence: tuple[EvidenceRef, ...] = ()

class AdmissionDisposition(str, Enum):
    ALLOW = "allow"
    DEFER = "defer"
    DENY = "deny"
    ESCALATE = "escalate"

@dataclass(frozen=True, slots=True, init=False)
class ActionAdmissionDecision(Params):
    disposition: AdmissionDisposition
    reason: DecisionReason
    evaluator: str
    evidence: tuple[EvidenceRef, ...] = ()
    retry_at: datetime | UnsetType = Unset
    approval: ApprovalRequest | UnsetType = Unset
```

`execution_scope` is a closed tagged union and names the owner of this operation even in a minimal,
in-memory SDK call. `EphemeralOperationScopeRef.scope_id` is correlation only and can never be
resolved as a Run. `DurableRunScopeRef.run_id` is the sole Run claim; its optional
`owner_attempt_id` identifies the already-created enclosing OperationAttempt, not the action
attempt being minted. Separate request/context `run_id` and parent-attempt fields are forbidden,
so there is no duplicate scalar to reconcile. The scope contract lives below StateDB and can
therefore be created without importing persistence. A composition root that advertises history,
resume, scheduling, or certifiable Run evidence must resolve the durable scope through the
canonical Run repository from ADR-0123 before execution.

`ExecutionContext` is created by a trusted ingress/composition root, not deserialized from the
same request it authorizes. It binds the executor instance to the authoritative tenant and
principal (or an explicit anonymous/local principal), authentication/provenance evidence, exact
policy snapshot reference, execution scope, optional canonical Run, and selected effect backend.
`principal_claim` is untrusted policy input until the context binds or rejects it. Supplying an
actor label is never proof of authentication, preserving ADR-0087's epistemic boundary. A context
whose scope or Run disagrees with the request fails before policy evaluation.

Callers never construct or pass ADR-0087 `OperationContext` independently. Only the governed path,
after preflight, calls ActionExecutor's private `OperationContextFactory`. It derives the record
from exactly six explicit inputs: the trusted `ExecutionContext`, immutable `ExecutionRequest`,
resolved `ActionDescriptor`, exact activated `PolicySnapshot`, verified `ActionPreflightReport`,
and active `GovernanceBinding`. The preflight report
contains the selected effect-backend identity, complete capability report/digest, and capacity
lease; the boundary binding supplies the stable `task_boundary_id` and its one `chain_id`. The
factory mints only `operation_id`, derives `actor` only from `authenticated_principal`, derives an
optional canonical Run and enclosing attempt solely from `execution_scope`, and binds Session,
tool/action identity, policy version/digest, and the verified backend/capability digest. Any
mismatch among the six inputs fails before `OPERATION_STARTED`.
ADR-0087's OperationContext is therefore the frozen governed-evidence projection of this context,
not a peer authority or caller override. Minimal and durable-ungoverned profiles continue with
`ExecutionContext` only; they construct no ADR-0087 OperationContext, append no governed evidence,
and make no certificate claim.

Semantics are closed:

- `ALLOW` permits the current normalized request at this stage;
- `DEFER` is a capacity/quota/deadline scheduling result and may include `retry_at`;
- `DENY` is a terminal policy result for this attempt;
- `ESCALATE` requests approval from a named authority. It is not an implicit allow and does not
  block a worker while awaiting a human;
- an evaluator exception is not an ordinary DENY unless the declared security policy maps that
  evaluator failure to a fail-closed denial;
- `Unset` means inherit/unresolved where inheritance is allowed; `None` means explicitly disabled
  only for fields whose contract declares that meaning.

Capacity admission and security authorization can use the same decision value, but they are
separate named evaluators and stages. The old `Processor.request_permission()` is renamed or
adapted as capacity admission; it is not security evidence.

The ADR-0087-to-executor reducer is closed:

- a hard gate deny becomes `DENY`;
- an unresolved soft deny appends `OPERATION_DENIED`, then becomes `ESCALATE` when a named approval
  authority exists and otherwise `DENY`;
- a soft deny with a prevalidated, already recorded exception becomes `ALLOW` and permanently
  degrades any later certificate;
- an advisory deny becomes `ALLOW` carrying the denying `GateResult` evidence reference;
- all applicable allows become `ALLOW`;
- capacity/quota returns `DEFER`; it is neither a `GateResult` nor a certificate fact.

Gate-derived escalation is a terminal denial of the current governed operation. Approval resolution
starts a new attempt and new request identity; it never wakes the old worker or changes the
`OPERATION_DENIED` record into an allow.

### D4 — one compiled plan defines the enforcement order

The operation owner compiles an immutable `ActionExecutionPlan` before any side effect. The fixed
stage order is:

1. resolve the ActionDescriptor and capture version/source provenance;
2. parse and schema-normalize the request without executing user code;
3. bind the trusted ExecutionContext, retrieve/verify the exact activated policy snapshot when
   governed, probe the selected effect backend against its required containment rules, and acquire
   a bounded capacity/deadline admission lease; unsupported required containment returns `DENY`
   and unavailable capacity returns `DEFER`, both before opening a governed operation;
4. in governed mode only, derive ADR-0087 OperationContext, append `POLICY_BOUND` and
   `OPERATION_STARTED`, and require both receipts; ungoverned profiles skip this stage without
   fabricating policy, boundary, chain, or evidence values;
5. evaluate Session/tenant/principal authorization;
6. apply invocation-specific intrinsic guards owned by the action, already-selected effect backend,
   or transport;
7. evaluate the resolved action/run policy;
8. run explicitly configured user transformations;
9. normalize and schema-validate the transformed request;
10. re-run every security evaluator whose inputs may have changed;
11. append each governed `GATE_EVALUATED`, exception, and denial record as it is produced and
    complete the pre-call evidence checkpoint;
12. release the executor's private one-use invocation token and invoke exactly once;
13. normalize success or captured failure into an `ExecutionOutcome` and append the matching
    governed terminal record;
14. persist Run/message/outbox state according to profile: governed execution projects only from
    verified ADR-0087 append receipts; durable-ungoverned execution commits its authoritative typed
    attempt/outcome and outbox intent in one StateStore transaction; minimal execution has no
    durable projection requirement. Post/error observations are emitted only from the applicable
    committed authority.

Stages 5–10 use ADR-0120's ordered interceptor mechanics with domain-specific typed contexts. A user
transformation cannot remove or reorder security stages. Each security decision is bound to a
canonical digest of the arguments it evaluated. Step 10 is skipped only when the digest and all
policy inputs are unchanged, and that fact is recorded.

In governed mode evidence append is control flow, not post-processing. Every required pre-call
append must return ADR-0087's `AppendReceipt` before stage 12. Failure raises its
`GovernanceEvidenceError(call_executed=False)` and the callable is unreachable. Completion/failure
append uses ADR-0087's after-call distinction: an evidence failure reports `call_executed=True`
and never retries the side effect. Cancellation, timeout, and an ambiguous external result append
new closed terminal evidence types `OPERATION_CANCELLED`, `OPERATION_TIMED_OUT`, and
`EXTERNAL_RESULT_AMBIGUOUS`; ADR-0087's certificate completeness set is amended to include them.
`DEFER` and preflight containment `DENY` occur before `OPERATION_STARTED`. A gate-derived
`ESCALATE` occurs after start, closes the current operation with `OPERATION_DENIED`, and can become
certifiable as a faithfully denied operation; approval creates a new attempt.

Retries create new attempt identity. An idempotency policy may reuse an external idempotency key,
but it never re-enters the invocation stage of the same attempt after an ambiguous result without
a transport-specific reconciliation decision.

Cancellation propagates through the executor. Only bounded evidence/teardown work may be shielded
using LionAGI concurrency primitives. Raw `asyncio.create_task`, `asyncio.Queue`, and direct
semaphore/counter mutation are not part of the public executor contract.

### D5 — permission policy is declared, resolved, and snapshotted

`PermissionPolicy` becomes an immutable `Params`/`Spec`-composed declaration and compiler input.
The runtime output is the exact activated ADR-0087 `PolicySnapshot`; there is no second resolved
policy object, store, or digest. Every executable rule must be represented losslessly in the
snapshot's closed `CompiledPolicyRule` union, referenced by its gate binding, and covered in full
by its digest; out-of-band executable rules are forbidden.
The declaration contains typed rules, not executable callbacks. Rules identify:

- principal and tenant scope;
- action/tool allow and deny patterns;
- workspace read/write roots;
- network destinations and modes;
- subprocess policy;
- MCP server/tool/resource grants;
- secret/environment exposure policy;
- approval requirements and approver class;
- budgets, expiry, and provenance.

Policy composition has one precedence rule: explicit deny dominates allow; narrower scoped rules
may reduce but cannot silently expand a parent policy; expansion requires an identified authority
and evidence. The resolved snapshot has no `Unset` values and is immutable. Its exact
`(policy_id, version, digest)` reference is attached to the execution scope and, when present, the
Run before execution.

AgentSpec serialization includes the declared policy in every supported format. Mapping input is
validated into `PermissionPolicy`; it is never silently ignored. Documentation, schema, and
round-trip tests are generated from the same declaration.

The compatibility `yolo` field is deprecated. During migration it compiles to one named legacy
profile per provider and produces a degradation warning. It is never treated as a portable proof
that a provider grants the same filesystem, network, approval, and subprocess behavior.

True permission inheritance uses `Unset` in a child declaration, resolves against a captured
parent policy, and stores the resolved snapshot. A literal `"inherit"` is not a policy preset.

### D6 — native coding agents use a provider-neutral harness contract

A **Harness** is the controlled launch and observation boundary around a native coding-agent
provider. It is not an Action and does not route the provider's internal tools through
ActionExecutor unless the provider explicitly delegates those calls back to LionAGI.

```python
@dataclass(frozen=True, slots=True, init=False)
class HarnessSpec(Params):
    provider: ProviderRef
    model: str | UnsetType = Unset
    workspace: WorkspacePolicy
    approval: ApprovalPolicy
    tools: ToolPolicy
    filesystem: FilesystemPolicy
    network: NetworkPolicy
    subprocess: SubprocessPolicy
    mcp: MCPGrantPolicy
    environment: EnvironmentPolicy
    resources: ResourceBudget
    required_capabilities: tuple[CapabilityRequirement, ...] = ()

class HarnessAdapter(Protocol):
    async def probe(self) -> ProviderCapabilitySet: ...
    def compile(
        self,
        spec: HarnessSpec,
        capabilities: ProviderCapabilitySet,
    ) -> HarnessPlan: ...
    async def spawn(self, plan: HarnessPlan, context: HarnessContext) -> HarnessHandle: ...
```

`HarnessPlan` is an immutable, redacted description of the exact provider version, executable,
argv/config, environment allowlist, workspace lease, declared controls, and expected evidence. It
is attached to the execution scope and, for a durable profile, the Run before spawn. Secrets are
referenced, not serialized into plans or logs.

Adapter probing is versioned and cached only with provider executable/version identity. A static
assumption about a provider option is not sufficient evidence when the local provider version can
change its meaning.

### D7 — admission, effect containment, and provider controls remain explicit

| Plane | Authority | What it can prove |
|---|---|---|
| Harness spawn admission | LionAGI policy and composition root | LionAGI decided whether and how to launch the provider |
| Provider-native sandbox/approval | Provider plus OS/container backend | Which declared controls the provider/backend reports as enforced |
| LionAGI Action admission/invocation | ActionExecutor | LionAGI authorized and invoked a local/MCP Action through its controlled path |
| Local Action effect containment | selected `ActionExecutionBackend` plus named OS/container/remote authority | Which filesystem, network, subprocess, environment, and secret-exposure constraints the backend actually enforces |

`ActionExecutionBackend` has a provider-independent `probe()` and `invoke()` port and returns a
versioned `ActionBackendCapabilityReport` before invocation. The ordinary in-process Python backend
reports effect containment as unsupported: an argument/path guard is admission evidence, not proof
that arbitrary callable code could not access another path or open a socket. A required containment
capability that the selected backend cannot enforce returns `DENY` before `OPERATION_STARTED`.
Optional admission-only restrictions may proceed only when policy labels the weaker claim and the
outcome/report preserve that degradation. This report is distinct from the native-harness
`CapabilityReport`; neither subclasses a generic catch-all capability authority.

The executor freezes the verified result together with its admission lease before deriving an
OperationContext:

```python
@dataclass(frozen=True, slots=True, init=False)
class ActionPreflightReport(Params):
    report_id: IDType
    request_id: IDType
    action_descriptor_digest: Digest
    effect_backend: ActionBackendRef
    capability_report: ActionBackendCapabilityReport
    capability_digest: Digest
    admission_lease: AdmissionLeaseRef
    verified_at: datetime
    expires_at: datetime | UnsetType = Unset
```

ActionExecutor creates this value from its selected backend and capacity-admission port; callers
cannot submit one. Its digest covers the entire capability report, backend/version identity,
action descriptor, request, lease, and expiry. An expired lease, changed backend report, or digest
mismatch fails before `OPERATION_STARTED`.

A provider-native tool call does not become a LionAGI Action merely because its stream uses the
words `tool_use` or `tool_result`. The adapter emits an observation with provider event identity,
reported arguments/results subject to redaction, and capability/evidence references. It must not
emit or satisfy an ADR-0087 `GateResult`, governed evidence record, or `TaskCertificate`.

If a provider supports calling a LionAGI MCP server, the MCP server independently routes each
LionAGI-owned action through ActionExecutor. Provider approval and LionAGI authorization are both
required when both planes apply.

Worktrees, subprocesses, and path checks are isolation/convenience mechanisms unless a named OS or
container backend attests stronger enforcement. Documentation and UI must not call them a sandbox
without that capability evidence.

### D8 — capability negotiation is typed and fails closed

Compilation produces a report:

```python
@dataclass(frozen=True, slots=True, init=False)
class CapabilityReport(Params):
    provider: ProviderRef
    provider_version: str
    enforced: tuple[CapabilityResult, ...]
    degraded: tuple[CapabilityResult, ...]
    unsupported: tuple[CapabilityResult, ...]
    probe_evidence: tuple[EvidenceRef, ...]
```

Rules:

- every requested control appears exactly once in enforced, degraded, or unsupported;
- an unsupported or degraded **required** capability prevents spawn;
- an optional degradation requires an explicit policy opt-in and is persisted on the Run;
- omission, unknown provider version, or adapter compilation failure is unsupported, not allow;
- provider claims and OS/backend evidence are labeled separately;
- the report and plan are canonically serializable, redacted, and hashable under ADR-0119;
- capability names are namespaced and versioned; they are not an untyped set of strings.

`session/capabilities.py` currently describes structured-emission schemas. It is renamed to an
emission contract or moved to the owning operation; it does not become a security capability
authority because of its filename.

### D9 — execution types are narrowed instead of unified by inheritance

The word Executor is reserved for a component that owns the complete lifecycle of one kind of
execution. Names become explicit:

| Current concept | Target responsibility |
|---|---|
| generic `Processor` / `Executor` | `EventDriver`: worker queue and executable Event lifecycle |
| `RateLimitedAPIExecutor` | service request driver composed with capacity admission |
| `ActionManager` | registry/resolver only |
| `ActionExecutor` | governed callable/MCP invocation authority |
| flow dependency/reactive executors | flow scheduler/driver; compose common task/concurrency primitives |
| persistence “executor” wording | `StateStore` / `TransactionRunner` |
| native coding provider | `HarnessAdapter` + provider process handle |

`iModel` stops reaching into queue, semaphore, and worker counters. It calls an explicit service
port. Shared internal primitives may provide task groups, queues, deadlines, and cancellation, but
inheritance does not imply shared policy semantics.

### D10 — outcomes and evidence are first-class

`ExecutionOutcome` is a closed tagged union for success, denial, deferral, escalation, captured
failure, cancellation, timeout, and ambiguous external result. It distinguishes a successful
`None` return from a failure and includes no raw exception or secret by default.

Every attempt can be audited as:

```text
request digest
  -> resolved action + schema version
  -> ordered admission/policy decisions
  -> optional transformation digest change
  -> revalidation/recheck decisions
  -> invocation receipt or explicit no-invocation reason
  -> normalized outcome
  -> observation and durable evidence references
```

Governed evidence is committed only through ADR-0087 `EvidenceStore`. In that profile,
StateStore/outbox persists canonical Run projections, attempt/outcome records, messages, and
evidence references only after the corresponding verified `AppendReceipt`. One backend may
implement both ports, but cross-store atomicity is not assumed or fabricated. Observation or
Run-projection failure cannot veto or erase an evidence append. Conversely, a required pre-call
evidence failure prevents invocation; a post-call append failure preserves ADR-0087's explicit
`call_executed=True` uncertainty rather than rewriting the action outcome or retrying.

Durable-ungoverned execution has no evidence receipt to project and makes no governance or
certificate claim. Its authoritative typed attempt, normalized outcome, message changes, and
outbox intents commit atomically through StateStore under the canonical Run/OperationAttempt
identity. Minimal execution returns its typed outcome without promising either durable authority.

When governed evidence and StateStore are separate, a durable profile that claims Run or
OperationAttempt projections must also provide ADR-0087's `EvidenceProjectionSource`. Every
successful append becomes discoverable through its at-least-once durable cursor. An
`EvidenceProjector` reads receipts/records after the last cursor and applies each projection with
idempotency key `(chain_id, record_id, projection_version)`; the projection rows and advanced
cursor commit in one StateStore transaction. A crash before that transaction replays the record;
a duplicate delivery is a no-op after digest comparison; a conflicting digest fails closed and
raises operator attention. Synchronous stage 14 may reduce latency, but it uses the same
idempotency key and never substitutes for the projector. If projection is unavailable after a side
effect, the execution response exposes `projection_pending`/`call_executed=True`; restart recovery
drains the durable source rather than inferring an outcome. A backend unable to provide durable
enumeration cannot advertise the durable-governed profile.

## Placement

The target dependencies are:

```text
lionagi/ln/types + concurrency + serialization
  -> lionagi/protocols/execution/       # request, decisions, outcome, ports
  -> lionagi/service/actions/           # ActionExecutor implementation
  -> lionagi/agent/harness/              # neutral specs and provider adapter protocol
  -> lionagi/providers/*/harness.py      # provider-specific compilation/spawn
  -> CLI / MCP / Studio composition roots
```

Policy declarations belong in contracts, not CLI or Studio. Provider flags belong only in
provider adapters. UI and CLI consume generated schemas/projections and display capability
degradation explicitly.

## Migration plan

### Phase 0 — characterize and close false claims

- enumerate every callable/MCP/direct-manager invocation route and add a bypass test matrix;
- characterize current hook order, denial shapes, successful-`None`, captured failure,
  cancellation, retries, and message/evidence behavior;
- inventory AgentSpec/PermissionPolicy formats and add negative tests for silently ignored policy;
- inventory every provider's current `yolo` mapping and tool-event semantics;
- freeze ADR-0087 evidence-chain/certificate fixtures and map each clause through the reconciliation
  table above;
- correct stale ADR-0012/ADR-0044/ADR-0086 implementation-status text;
- remove or correct the unsupported subagent `"inherit"` option in an independent narrow fix.

No enforcement order is changed in Phase 0.

### Phase 1 — declarations and compatibility adapters

- land immutable ExecutionRequest, ExecutionContext, ActionAdmissionDecision, ExecutionOutcome, policy
  declarations, HarnessSpec, HarnessPlan, provider CapabilityReport, and
  ActionBackendCapabilityReport values using ADR-0119;
- extend ADR-0087's terminal evidence taxonomy for cancellation, timeout, and ambiguous external
  results; retain its canonical chain, store, exact policy pin, and certificate verifier;
- implement current Processor boolean and existing hook/preprocessor behavior as named adapters;
- make permission serialization lossless and reject invalid mappings;
- add redaction/canonical-digest tests.

### Phase 2 — authoritative local/MCP actions

- introduce ActionExecutor behind the existing `_act` path;
- make `ActionManager.invoke()` and FunctionCalling compatibility methods delegate to it;
- move plugin materialization before plan compilation;
- enforce transform-normalize-recheck order;
- require pre-call ADR-0087 append receipts in governed mode and preserve its post-call evidence
  failure distinction;
- select/probe the local effect backend and fail closed for required containment it cannot report;
- close all direct invocation routes with import/static checks and runtime tests;
- project append receipts and outcomes to StateStore/outbox where durable Run mode is enabled.

The phase is not complete while a public production caller can reach the underlying callable or
MCP transport without ActionExecutor.

### Phase 3 — native harness adapters

- implement provider capability probes and plan compilers one provider at a time;
- replace portable `yolo` semantics with explicit profiles and degradation reports;
- attach the redacted plan, capability report, and resolved policy to the execution scope and to
  canonical Run when durable mode is selected;
- distinguish provider-native tool observations from LionAGI Action evidence;
- integrate workspace leases and OS/container backends without upgrading their claimed guarantee.

A provider is enabled for a requested profile only after its required-capability matrix passes.

### Phase 4 — narrow and delete

- rename/narrow generic Processor and flow/state types without compatibility breakage;
- remove construction-time policy hook attachment after all consumers use compiled plans;
- remove direct invocation APIs after a documented deprecation window;
- delete legacy `yolo` and duplicated provider mappings after saved configs are migrated;
- delete compatibility denial/error adapters once all supported callers consume typed outcomes.

## Acceptance gates

1. **No bypass:** static and runtime matrices prove every LionAGI-owned callable and MCP action
   enters ActionExecutor exactly once.
2. **No stale authorization:** any argument/policy mutation after a decision forces normalization
   and security recheck before invocation.
3. **Typed lifecycle:** allow, defer, deny, escalate, successful `None`, failure, cancellation,
   timeout, and ambiguous results have distinct stable outcomes.
4. **Policy parity:** AgentSpec policy round-trips through all supported formats and invalid
   mappings fail loudly.
5. **Capability honesty:** every requested harness control is enforced, degraded, or unsupported;
   required degradation/absence fails before spawn.
6. **Evidence integrity:** a provider-native observation cannot satisfy a LionAGI authorization
   assertion; governed evidence is redacted, hash-chain verified, and bound to
   request/plan/policy digests. Every injected pre-call append failure prevents invocation; every
   post-call append failure reports `call_executed=True` and never retries.
7. **Scope and Run binding:** every request has one closed tagged `ExecutionScopeRef`. Ephemeral SDK
   scopes carry no Run claim; a durable scope contains the only `run_id` and optional enclosing
   attempt, and binds resolved policy, optional HarnessPlan/CapabilityReport, attempts, and
   outcomes to the one canonical Run identity defined by ADR-0123.
8. **House rules:** executor/harness production paths use LionAGI concurrency, serialization, and
   schema primitives; raw lower-level use is confined to those internal libraries/adapters.
9. **Compatibility:** existing supported SDK entry points delegate through the authority during
   migration and have explicit deprecation tests.
10. **Negative growth:** each migration phase removes the replaced enforcement path in the same
    or immediately following phase.
11. **Trusted identity:** request principal/tenant claims cannot override ExecutionContext; missing,
    mismatched, unauthenticated, and cross-scope cases fail before policy evaluation. A supplied
    ADR-0087 OperationContext is rejected, while the private factory binds actor/scope/Run/backend/
    policy fields exactly from trusted inputs.
12. **Effect honesty:** the in-process backend reports containment unsupported; required filesystem,
    network, subprocess, environment, or secret containment denies until a backend proves it.
13. **Certificate completeness:** success, captured failure, denial, cancellation, timeout, and
    ambiguous-result operations each close with exactly one allowed ADR-0087 terminal record;
    preflight defer/containment denial creates no `OPERATION_STARTED`; gate-derived escalation
    closes the current operation with `OPERATION_DENIED`, and any approved retry is a new attempt.
14. **Projection recovery:** crash injection after every governed evidence append and before/after
    StateStore commit proves at-least-once discovery, idempotent replay, atomic cursor advancement,
    conflict attention, and eventual Run/OperationAttempt projection without repeating a callable.

## Issue disposition

This ADR consolidates #1196, #1381, #1973, #3028, #3130, #3194, and the authoritative-controller
slice of #2161. #1393 is narrowed to evidence storage and TaskCertificate policy; its remaining
governance work consumes this executor rather than making enforcement optional.

Adjacent issues retain separate owners:

- #1971 decides naming/semantics and must not unify structured-emission grants, tool permissions,
  harness capabilities, and worker resource requirements under one `Capability` base;
- #1195 remains an execution-target/backend issue after the harness seam, while #1382 is a later
  measurement/benchmark rather than an enforcement prerequisite;
- #2069 owns MCP peer/transport authentication, which action authorization cannot replace;
- #2394 owns service request admission/deadline/resilience outside the Action lifecycle;
- #2921 implements accepted MCP qualified-name behavior, which the harness consumes;
- #2387 is rescoped from one pinned provider version to provider-adapter translation of typed
  permissions;
- #2653, #2664, #2932, #2956, #3189, and related Run evidence work consume ADR-0123 rather than
  becoming harness internals;
- #3066 and #3129 remain scheduler/process-lifecycle correctness work and do not wait for this ADR.

Credential and secret-redaction bugs remain immediate fixes. Issues already fixed on main or
owned by an open PR are verified and closed rather than copied into this ADR's breakdown.

After acceptance, implementation is split by phase and enforcement boundary. No issue may add a
new direct invocation route, provider-wide boolean permission shortcut, or untyped capability set.

## Consequences

### Positive

- Policy cannot be bypassed by selecting a different LionAGI call path.
- Capacity deferral, security denial, approval escalation, and execution failure become explicit.
- Native provider limits are represented honestly instead of normalized behind `yolo`.
- AgentSpec, CLI, Studio, and persisted Run evidence can consume one declared policy/harness
  schema.
- Manager, executor, flow scheduler, state store, and harness roles become smaller and testable.

### Costs and risks

- Closing direct SDK paths is a compatibility migration and may expose callers that relied on
  undocumented invocation behavior.
- Provider capability probing varies by version and requires conservative maintenance.
- Immutable request/outcome evidence adds objects and persistence volume; redaction and retention
  remain mandatory.
- Human approval requires an asynchronous control-plane protocol; workers cannot simply wait on a
  callback indefinitely.
- An ActionExecutor is only authoritative for actions it owns. It cannot retrofit enforcement into
  opaque provider-internal tools.

## Alternatives rejected

### Keep attaching preprocessors to each Tool

Rejected because construction and plugin paths can omit attachment, ordering differs, and mutable
tools cannot prove which policy guarded one invocation.

### Make SessionObserver or HookBus the permission authority

Rejected because observation must not suppress audit delivery, and a general dispatcher does not
own resolution, revalidation, invocation, or atomic evidence.

### Route every provider-native tool event through ActionExecutor after the fact

Rejected because post-hoc observation cannot authorize an action that already happened. Only a
provider callback that blocks and delegates before execution can join that path.

### Treat all Executor classes as one inheritance hierarchy

Rejected because queue driving, flow scheduling, database transactions, provider processes, and
governed callable invocation have different state machines. They may compose common primitives and
ports without sharing a misleading base policy.

### Define safe/read-only/yolo as portable provider presets

Rejected because providers and OS backends enforce different capabilities. A neutral request plus
typed capability/degradation report is the portable contract.

### Wait for a perfect OS sandbox before defining the harness

Rejected because honest capability reporting permits useful execution now without overstating its
guarantees, and provides the seam for stronger backends later.
