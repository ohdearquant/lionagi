# ADR-0073: Universal Agent Spec — Profile Composition, Capability Ontology, Loop Control

**Status**: accepted (design contract for the `agent-spec-wiring` show)
**Date**: 2026-05-30
**Builds on**: ADR-0071 (Cognitive Mode Model), ADR-0072 (Reactive Capability Bus)

## Context

lionagi has, as of v0.26.12, three powerful but **disconnected** primitives:

1. **Casts** (ADR-0071): 41 `Role`s + 14 `Mode`s as composable frozen patterns,
   plus a `Pack`/`RolePolicy` carrying per-role authority / boundaries /
   escalation conditions. `AgentConfig.build_system_message()` composes
   `role + modes + system_prompt`.
2. **Reactive capability bus** (ADR-0072): a `Signal`/`StructuredOutput`
   envelope, a `SessionObserver` that dispatches by payload *type* or *field
   value*, `branch.grant_capabilities(operable)`, and run-lifecycle signals
   (`RunStart`/`RunEnd`/`RunFailed`).
3. **Permission system**: `PermissionPolicy` (allow/deny/escalate, fnmatch
   rules) wired as a `security_pre` hook through `create_agent()`.

These never meet. The CLI orchestration path (`li o flow` / `li o fanout`)
runs entirely through the thin `AgentProfile` (`cli/_agents.py`): a worker's
`role` is a *profile-file name*, not a casts `Role`. CLI workers get no modes,
no permission policy, no capability grant, and the bus — though live — is never
fed because nobody calls `grant_capabilities()` on a worker branch. The
`RolePolicy` escalation conditions are dead data.

Three structural gaps follow:

- **No universal agent spec.** Identity composition (`AgentConfig`) and CLI
  orchestration (`AgentProfile`) are parallel models that don't share a type.
- **No capability ontology.** Each role's *Artifacts* section already
  describes what it produces (a critic yields a verdict; a researcher yields
  findings) — but nothing turns that into typed signals the session observes.
- **The bus can observe but cannot steer.** `emit` runs handlers, but a
  handler cannot stop, cancel, or break the in-flight run loop. We built the
  afferent nerve (sensing) without the efferent nerve (acting).

## Decision

Introduce a **universal `AgentSpec`**, a **capability ontology** derived from
the roles themselves, and **loop control** that lets observers steer a run.
Four layers, each a play in the show; this ADR is the contract they build to.

---

### Layer 1 — Capability ontology (`lionagi/casts/capabilities.py`)

A capability is what a role *produces*, expressed as a typed payload model.
The role's *Artifacts* section is the source of truth — we formalize it.

**Capability payload models** (Pydantic `BaseModel`s, one per produced thing).
Field types are the contract; keep them small and observable:

```python
class Finding(BaseModel):
    description: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    severity: str | None = None          # CRITICAL | MAJOR | MINOR | None
    evidence: str | None = None
    source: str | None = None            # file:line, URL, doc ref

class Verdict(BaseModel):
    verdict: str                          # APPROVE | APPROVE_WITH_FIXES | REQUEST_CHANGES | REJECT
    rationale: str
    evidence: str | None = None
    reversible_by: str | None = None      # what evidence would reverse a REJECT

class ComplianceVerdict(BaseModel):
    verdict: str                          # BLOCK | PASS
    control: str                          # the control citation
    evidence_refs: list[str] = Field(default_factory=list)

class RiskAssessment(BaseModel):
    failure_mode: str
    likelihood: float = Field(ge=0.0, le=1.0)
    impact: float = Field(ge=0.0, le=1.0)
    mitigation: str | None = None

class AnalysisResult(BaseModel):
    metric: str
    value: float
    ci_95: tuple[float, float] | None = None
    p_value: float | None = None

class Conflict(BaseModel):              # researcher's conflict register
    sources: list[str]
    nature: str

class Gap(BaseModel):                   # coverage / unknowns
    area: str
    what_is_unknown: str

class ExecutionPlan(BaseModel):         # orchestrator / strategist / planner
    steps: list[str]
    dependencies: list[str] = Field(default_factory=list)
    exit_criteria: str | None = None

class ComplexityScore(BaseModel):       # strategist's C(τ)
    score: float = Field(ge=0.0, le=1.0)
    rationale: str

class ArtifactProduced(BaseModel):      # implementer / writer / migrator / ...
    path: str
    kind: str                            # code | doc | test | schema | script | ...
    description: str | None = None
    verified: bool = False

class VerificationResult(BaseModel):    # tester / implementer
    suite: str
    passed: bool
    coverage: float | None = None
    gaps: list[str] = Field(default_factory=list)

class EscalationRequest(BaseModel):     # UNIVERSAL — every role can escalate
    reason: str
    context: dict = Field(default_factory=dict)
    blocking: bool = True
    from_role: str | None = None
```

**Role → capability map.** Drawn from each role's *Artifacts* + *escalation*
language. `EscalationRequest` is appended to **every** role automatically.

```python
ROLE_CAPABILITIES: dict[str, tuple[type[BaseModel], ...]] = {
    # Verdict-zone
    "critic":     (Verdict, Finding),
    "reviewer":   (Verdict, Finding),
    "auditor":    (ComplianceVerdict, Finding),
    "arbitrator": (Verdict,),
    "evaluator":  (Verdict, Finding),
    "tester":     (VerificationResult, Finding),
    # Discovery-zone
    "researcher":     (Finding, Conflict, Gap),
    "analyst":        (AnalysisResult, Finding),
    "explorer":       (Finding, Gap),
    "investigator":   (Finding, Gap),
    "troubleshooter": (Finding,),
    "assessor":       (RiskAssessment, Finding),
    "contrarian":     (Finding,),
    "commentator":    (Finding,),
    "synthesizer":    (Finding, Conflict),
    # Plan-zone
    "orchestrator": (ExecutionPlan,),
    "strategist":   (ComplexityScore, ExecutionPlan),
    "planner":      (ExecutionPlan,),
    "coordinator":  (ExecutionPlan,),
    "architect":    (ExecutionPlan, ArtifactProduced),
    "modeler":      (ArtifactProduced,),
    "innovator":    (Finding,),
    "suggester":    (Finding,),
    # Build-zone
    "implementer": (ArtifactProduced, VerificationResult),
    "prototyper":  (ArtifactProduced, VerificationResult),
    "refactorer":  (ArtifactProduced, VerificationResult),
    "migrator":    (ArtifactProduced, VerificationResult),
    "deployer":    (ArtifactProduced,),
    "operator":    (ArtifactProduced,),
    "writer":      (ArtifactProduced,),
    "translator":  (ArtifactProduced,),
    "scribe":      (ArtifactProduced,),
    "curator":     (ArtifactProduced,),
    # Process-zone
    "facilitator":    (Finding,),
    "negotiator":     (Finding,),
    "mentor":         (Finding,),
    "persona":        (Finding,),
    "responder":      (Finding,),
    "postmortem_lead":(Finding,),
    "entrepreneur":   (Finding,),
}
```

**Builder.** Turns a role name into the `Operable` `grant_capabilities`
consumes. Field name = snake_case of the model class name (`Finding` →
`finding`, `ComplianceVerdict` → `compliance_verdict`).

```python
def capability_models(role: str) -> tuple[type[BaseModel], ...]:
    """Capability payload types for a role, including EscalationRequest."""
    base = ROLE_CAPABILITIES.get(role, ())
    return (*base, EscalationRequest) if EscalationRequest not in base else base

def capability_operable(role: str) -> Operable | None:
    """Build the Operable grant for a role. None when the role has no
    capabilities mapped (then the caller skips grant_capabilities)."""
    # Errata (PR #1211): the earlier pseudocode used `if not models: return None`,
    # which can never be True because capability_models() always returns at least
    # (EscalationRequest,).  The correct guard is `if role not in ROLE_CAPABILITIES`,
    # which matches the implementation in lionagi/casts/capabilities.py:203.
    if role not in ROLE_CAPABILITIES:
        return None
    models = capability_models(role)
    specs = tuple(Spec(m, name=_field_name(m)) for m in models)
    return Operable(specs, name=f"{role}_capabilities")
```

`_field_name(model)` converts `ComplianceVerdict` → `compliance_verdict` via a
simple CamelCase→snake_case helper. Keep the map of model→field deterministic
and round-trippable (observers reference the *type*, not the field name, so the
exact string only matters for the emitted JSON key the model sees).

**Why this is the right cut.** The observer registry IS the capability registry
(ADR-0072): a capability is live iff something observes its type. We are not
introducing a central enum of "what may be emitted" — `ROLE_CAPABILITIES` is a
*default suggestion* per role, fully overridable on the spec. A user can grant a
researcher the `Verdict` capability if they want; nothing forbids it.

---

### Layer 2 — Loop control (`lionagi/session/control.py`)

The bus senses; loop control acts. v1 scope is deliberately small and *honest*
— only directives the CLI stream loop can honor cleanly at a chunk boundary.

```python
class LoopDirective(Enum):
    CONTINUE = "continue"   # default / no-op
    CANCEL   = "cancel"     # stop consuming the stream, flush partial, clean exit
    BREAK    = "break"      # stop and raise LoopBreak (surfaces as RunFailed)

@dataclass(frozen=True, slots=True)
class LoopControl:
    directive: LoopDirective
    reason: str | None = None

class LoopBreak(Exception):
    """Raised inside the run loop when an observer issues BREAK."""
    def __init__(self, reason: str | None = None):
        super().__init__(reason or "loop broken by observer")
        self.reason = reason
```

**PAUSE and INJECT are explicitly OUT of v1.** Pausing/resuming or injecting an
instruction into a live CLI subprocess stream is a turn-boundary concern, not a
chunk-boundary one; doing it half-way produces a stub. Document them in the ADR
as future work; do **not** add them to the enum (no-stubs rule).

**Branch wiring** (`session/branch.py`):

```python
_loop_control: LoopControl | None = PrivateAttr(None)

def control(self, directive: LoopDirective, *, reason: str | None = None) -> None:
    """Queue a loop-control directive. Called from an observer handler to
    steer the in-flight run. The run loop polls this at chunk boundaries."""
    self._loop_control = LoopControl(directive, reason)

def poll_control(self) -> LoopControl | None:
    """Return and CLEAR any queued directive (one-shot)."""
    ctrl, self._loop_control = self._loop_control, None
    return ctrl
```

**Run-loop wiring** (`operations/run/run.py`, inside the `async for chunk`
loop). The seam is *immediately after each* `await _emit_message_signal(...)`
— at that point all observer handlers for the just-emitted signal have run and
may have called `branch.control(...)`. Add a single helper and call it after
each of the three `_emit_message_signal` sites and the final flush:

```python
def _check_control(branch) -> None:
    ctrl = branch.poll_control()
    if ctrl is None or ctrl.directive is LoopDirective.CONTINUE:
        return
    if ctrl.directive is LoopDirective.BREAK:
        raise LoopBreak(ctrl.reason)
    if ctrl.directive is LoopDirective.CANCEL:
        raise _StopStream(ctrl.reason)   # internal sentinel caught below
```

CANCEL must unwind to a *clean* exit: catch the internal `_StopStream` sentinel
just outside the `async for`, fall through to the existing `finally` (which
flushes accumulated text + persists the branch snapshot), and return normally
with the partial result. BREAK propagates `LoopBreak` out of `run()`; `operate`
already wraps the body and will emit `RunFailed(data=exc)` — that is the
intended surfacing.

**Closing the subprocess.** Breaking out of `async for chunk in model.stream(...)`
closes the async generator; verify the CLI provider terminates its subprocess
on generator close (it should, via the generator's `aclose`/`finally`). If a
provider leaks, that is a provider bug to file separately — the run-loop
contract is "stop consuming + clean up via finally".

---

### Layer 3 — Profile + AgentSpec composition

**`Profile` (`lionagi/casts/profile.py`)** — pure identity composition. A named
`Role` + ordered `Mode`s. Nothing runtime (no tools, model, permissions). This
is the `Pattern → Profile → Actor → Branch` progression's Profile node.

```python
@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    role: Role
    modes: tuple[Mode, ...] = ()

    def __post_init__(self):
        # Validate mode conflicts (ADR-0071 conflicts_with) at composition time.
        seen: dict[str, Mode] = {}
        for m in self.modes:
            for other in seen.values():
                if m.name in other.conflicts_with or other.name in m.conflicts_with:
                    raise ValueError(f"Mode conflict: {m.name} vs {other.name}")
            seen[m.name] = m

    @property
    def capabilities(self) -> tuple[type, ...]:
        from lionagi.casts.capabilities import capability_models
        return capability_models(self.role.name)

    def build_system_message(self) -> str:
        parts = [self.role.body] if self.role.body else []
        parts += [m.behaviors for m in self.modes if m.behaviors]
        return "\n\n".join(parts)

    @classmethod
    def compose(cls, role: str | Role, *, modes: list[str | Mode] | None = None,
                name: str | None = None) -> Profile:
        r = role if not isinstance(role, str) else Role.load(role)
        ms = tuple(m if not isinstance(m, str) else Mode.load(m) for m in (modes or []))
        return cls(name=name or r.name, role=r, modes=ms)

    @classmethod
    def from_yaml(cls, path) -> Profile:  # ~/.lionagi/profiles/<name>.yaml
        # {name, role, modes: [...]}
        ...
```

**Roster discovery** (`lionagi/casts/pattern.py`): add `list_roles()` and
`list_modes()` that enumerate the packaged `casts/roles/*.md` (excluding
`TEMPLATE`, excluding the `modes/` dir) and `casts/roles/modes/*.md`
respectively, merged with any user `~/.lionagi/roles/` and `~/.lionagi/modes/`.
These feed the orchestrator's planning roster.

**`AgentSpec` (`lionagi/agent/spec.py`)** — the universal runtime spec. Profile
(identity) + runtime concerns (model, effort, tools, permissions, capability
grant). This is what every surface composes to and what builds a Branch.

```python
@dataclass
class AgentSpec:
    profile: Profile
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] = ()                      # tool suites: "coding", "reader", ...
    permissions: PermissionPolicy | None = None
    grant_capabilities: bool = True                  # auto-grant the profile's capabilities
    pack: str | Pack | None = "default"              # RolePolicy source for escalation prose
    lion_system: bool = True

    @classmethod
    def compose(cls, role, *, modes=None, model=None, effort=None,
                tools=(), permissions=None, pack="default",
                grant_capabilities=True) -> AgentSpec:
        prof = Profile.compose(role, modes=modes)
        perm = _resolve_permissions(permissions)     # str preset | dict | PermissionPolicy | None
        return cls(profile=prof, model=model, effort=effort, tools=tuple(tools),
                   permissions=perm, pack=pack, grant_capabilities=grant_capabilities)

    def build_system_message(self) -> str:
        """role + modes + RolePolicy operational block (authority/boundaries/
        escalations rendered as explicit STOP-and-escalate instructions)."""
        body = self.profile.build_system_message()
        policy_block = self._render_policy_block()   # from Pack.policy(role)
        return "\n\n".join(p for p in (body, policy_block) if p)

    def capability_operable(self) -> Operable | None:
        from lionagi.casts.capabilities import capability_operable
        return capability_operable(self.profile.role.name) if self.grant_capabilities else None
```

`_resolve_permissions`: `"safe"|"read_only"|"allow_all"|"deny_all"` → the
matching `PermissionPolicy` classmethod; a dict → `PermissionPolicy.from_dict`;
a `PermissionPolicy` → itself; `None` → `None`.

`_render_policy_block`: load `Pack` (default ships in `casts/packs/default.yaml`),
get `policy(role)`; render `authority`/`boundaries` as context and
`escalations` as: *"When any of these conditions occur, STOP and emit an
`escalation` capability with the reason: …"* — tying the dead `RolePolicy`
escalation prose to the live `EscalationRequest` capability.

**Bridge for back-compat.** `AgentConfig` stays; add
`AgentSpec.from_config(config)` and have `create_agent()` accept either. The
existing `AgentConfig.build_system_message()` already does role+modes — keep it
working; `AgentSpec` is the richer, orchestration-facing path.

---

### Layer 4 — CLI orchestration wiring

**`FlowAgent` (`cli/orchestrate/flow.py`)** gains optional composition fields:

```python
class FlowAgent(HashableModel):
    id: str
    role: str                                  # now a casts Role name (validated against list_roles())
    modes: list[str] = Field(default_factory=list, description="cognitive overlays; e.g. ['adversarial','systematic']")
    permissions: str | None = Field(default=None, description="preset: safe|read_only|allow_all|deny_all")
    model: str | None = None
    guidance: str | None = None
```

(`capabilities` is **not** a FlowAgent field — it is derived from `role`
automatically. Keep the planner's surface small; the ontology does the work.)

**Orchestrator planning prompt** (`_run_flow_inner`): replace the
`list_agents()` roster with the casts roster — each role's frontmatter
`description` + the available modes (name + one-line description) + the
permission presets. The orchestrator picks `role + modes + permissions` per
agent. Validation rejects unknown roles/modes and mode conflicts before
execution (fail-closed, like the existing dep-cycle check).

**`build_worker_branch` (`cli/orchestrate/_orchestration.py`)** is the single
seam. When a worker has a casts `role` (not a `~/.lionagi/agents/` profile):

1. `spec = AgentSpec.compose(role=agent.role, modes=agent.modes, model=…, permissions=agent.permissions)`
2. system prompt = `LION_SYSTEM_MESSAGE + spec.build_system_message()` (+ team
   section as today)
3. register `spec.tools` (default `("coding",)` for write-capable roles,
   none/read-only suites for discovery roles — derive a sensible default by
   zone, overridable)
4. apply `spec.permissions` (see adapter note)
5. after `session.include_branches(wb)`, if `op := spec.capability_operable()`:
   `wb.grant_capabilities(op)` — this lights up the bus for that worker.

Preserve the existing `~/.lionagi/agents/` profile path for backward compat:
if `resolve_worker_spec(role)` finds a profile, use it (today's behavior); only
fall to the casts `AgentSpec` path when no profile file matches. This keeps
every existing flow working unchanged.

**Permission adapter (`lionagi/agent/adapters/`)** — the hard part. CLI workers
run on provider backends (claude_code, codex) whose tools lionagi's
`security_pre` hook does **not** intercept; the provider enforces its own
permissions. v1 ships **one** adapter, `claude_code.py`:

```python
def translate_permissions(policy: PermissionPolicy) -> dict:
    """PermissionPolicy → claude_code endpoint kwargs (permission_mode + allow/deny lists)."""
    # allow_all  → {"permission_mode": "bypassPermissions"} (or yolo kwargs already used)
    # read_only  → deny editor/bash, allow reader/search
    # deny_all   → deny everything
    # rules      → map allow/deny fnmatch patterns to claude_code's permission format
```

For lionagi-native tools (when a worker uses `register_tools`/`CodingToolkit`
rather than provider-native), the existing `security_pre` hook path applies
unchanged. Document clearly which path a given worker takes. Other providers
(codex, openai) are follow-up adapters — out of scope for this show, noted as
future work (do not stub them).

**Observer wiring** (`_run_flow_inner`, before `session.flow(...)`): the
orchestrator may register reactions over the worker capabilities, e.g.

```python
session.observe(Verdict, lambda v, ctx: ...)          # route REJECT to re-plan
session.observe(EscalationRequest, lambda e, ctx: ...) # surface / re-assign
session.observe(Finding, Finding.q ...)                # high-confidence routing
```

v1 wires a minimal, useful default: observe `EscalationRequest` (log + record
into the flow's control stream) and `Verdict` (record). The full reactive
re-planning loop is a documented extension point, not required for this show.

---

## Consequences

**Positive**

- One universal `AgentSpec` across `li o flow`, `li o fanout`, `li agent`, and
  programmatic `create_agent`.
- The 41 roles / 14 modes / default pack become *live* in CLI orchestration.
- Capabilities are derived from roles automatically — a researcher emits
  findings, a critic yields verdicts, every role can escalate — and the session
  observes them in real time.
- The bus becomes bidirectional: observers can CANCEL/BREAK a run.
- `RolePolicy` escalation prose is wired to the `EscalationRequest` capability.

**Negative / risks**

- Permission parity across providers is partial (claude_code only in v1).
- Loop control v1 is CANCEL/BREAK only; PAUSE/INJECT deferred.
- Two role-name namespaces remain during transition (`~/.lionagi/agents/`
  profiles vs casts roles); resolved by precedence (profile file wins), to be
  unified later.

**Rejected alternatives**

- *Capabilities as a FlowAgent field*: rejected — bloats the planner surface;
  the ontology already knows what each role emits.
- *Profile carrying tools/permissions*: rejected — Profile is identity
  (frozen, composable, named); runtime concerns live on `AgentSpec`.
- *A central capability enum*: rejected per ADR-0072 — the observer registry IS
  the registry; `ROLE_CAPABILITIES` is an overridable default, not a gate.

## Implementation order (show plays)

1. **capabilities** — Layer 1 + Layer 2 (ontology + loop control). No CLI deps.
2. **composition** — Layer 3 (Profile, AgentSpec, roster discovery). Depends on 1.
3. **wiring** — Layer 4 (FlowAgent, build_worker_branch, planner prompt,
   claude_code permission adapter, minimal observer wiring). Depends on 2.
4. **review** — multi-perspective gate over the integration branch.
5. **pr-slice** — re-slice the integration branch into layered PRs with a
   strict merge sequence, codex-review each.

Each layer ships green + tested before the next builds on it.
