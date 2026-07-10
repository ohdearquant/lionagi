# ADR-0044: Agent prompt directives and executable permissions

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: agent-roles
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0074

## Context

LionAGI currently uses the word “policy” for two mechanisms with different force.

Casts `RolePolicy` contains free-form authority, boundary, and escalation strings. `AgentSpec`
renders them into the system prompt and the catalog exposes them as metadata. Nothing in that type
routes an escalation or prevents an operation.

Agent `PermissionPolicy` is executable. It evaluates a tool name, action, and arguments and returns
allow, deny, or escalate. The agent factory turns that policy into a Tool preprocessor. The
Session governance gate is another executable check before tool resolution.

The naming collision and current construction order create five concrete failures.

**P1 — prompt text can be mistaken for enforcement.** “Authority” and “runtime operational
envelope” imply a grant, but the strings are only model guidance. A model can ignore them, and no
runtime component parses them into a decision.

**P2 — permission coverage depends on registration source.** Built-in tools receive preprocessors
when they are registered. MCP tools are discovered afterward and receive none. A `deny_all` policy
can block a built-in reader while allowing a discovered MCP tool.

**P3 — argument transformation can invalidate an earlier decision.** The current factory correctly
runs security hooks again after user pre-hooks when any user hook exists. That ordering is
load-bearing and must survive a universal interceptor design.

**P4 — transport trust and invocation authorization are separate.** Trusting a command or URL
enough to discover MCP schemas does not grant the resulting tool permission to execute every
operation. Conversely, a tool allow rule does not authorize loading an untrusted transport.

**P5 — ordinary Tool and CodingToolkit paths are only partly uniform.** Both use the same pre/post
chain helpers, but error handlers are registered into spec/toolkit maps and are not installed on
`Tool` or invoked by `FunctionCalling`. Existing non-dict post results also bypass post handlers.

The target trust and invocation path is:

```text
MCP config discovery ──► MCPSecurityConfig ──► register Tool
                                                   │
all built-ins and MCP tools registered             │
                    └──────────────────────────────┘
                                   │
                                   ▼
                      attach one interceptor plan
                                   │
tool request ─► Session gate ─► security ─► transform ─► security ─► invoke
                                                           │
                                         success ─► post   │   error ─► error handlers
```

| Concern | Decision |
|---|---|
| Prompt guidance type | D1: casts exposes non-enforcing `RolePromptDirectives`; `RolePolicy` is a deprecated alias only. |
| Executable permission model | D2: `PermissionPolicy` is the typed per-tool allow/deny/escalate decision source with fail-closed validation. |
| Universal interception | D3: construction attaches one idempotent interceptor plan after every built-in and MCP Tool is registered. |
| Invocation state machine | D4: security runs before and after transformations; denial cannot be recovered by post/error hooks. |
| MCP trust | D5: `MCPSecurityConfig` is an explicit, fail-closed construction input independent of tool permission. |
| Governance boundary | D6: the Session pre-invocation gate remains an earlier executable control and prompt directives never join either enforcement path. |

This ADR does not decide:

- A policy language for centralized authorization or relationship-based access.
- How escalation requests are routed to a human or another agent. Prompt guidance may request an
  escalation emission; executable `on_escalate` is a caller-supplied callback.
- Per-role configuration precedence. ADR-0043 resolves which directives and PermissionPolicy reach
  construction.
- Session HookBus or service API hook ownership. ADR-0047 records those mechanisms.
- Operating-system sandboxing. Tool permission is one application boundary and does not replace
  process, filesystem, or network isolation.
- MCP protocol authentication. This ADR fixes local transport admission and post-discovery Tool
  authorization.

## Decision

### D1 — casts prompt guidance is `RolePromptDirectives`, not executable policy

The target casts contract is:

```python
@dataclass(frozen=True, slots=True)
class RolePromptDirectives:
    """Non-enforcing text rendered into a Role's system prompt."""

    # Legacy field names remain data-compatible. "authority" is guidance
    # about the model's decision scope, not an executable grant.
    authority: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    escalations: tuple[str, ...] = ()

# Deprecated import compatibility; no second model or behavior.
RolePolicy = RolePromptDirectives
```

`Pack` changes its canonical field and accessor names:

```python
@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    directives: dict[str, RolePromptDirectives]
    configs: dict[str, RoleConfig]

    def prompt_directives(
        self,
        role: str,
        /,
    ) -> RolePromptDirectives | None: ...
```

During migration, `Pack.policies` and `Pack.policy(role)` are deprecated read aliases to the same
objects. The YAML keys `authority`, `boundaries`, and `escalations` remain accepted so existing
packs do not change meaning. New documentation and catalog descriptions call them decision-scope
guidance, operating constraints, and escalation guidance.

Prompt rendering remains:

```text
## Authority
- <authority guidance>

## Operational Boundaries
- <boundary guidance>

## Escalation Conditions
When any condition occurs, STOP and emit an escalation_request with the reason:
- <escalation guidance>
```

Those headings are model-facing compatibility text. Public API descriptions must state that the
block is non-enforcing. Catalog output nests it under `directives` or labels the existing fields as
prompt directives; it must not call them granted permissions.

**Exact semantics.**

- Empty tuples render no section.
- If all tuples are empty, the directives block is empty.
- A missing Role entry renders no directives.
- The strings are not parsed into `PermissionPolicy` rules.
- `escalations` do not install an `on_escalate` callback or a routing target.
- `RolePolicy` and `RolePromptDirectives` refer to one data model during migration. They may not
  diverge or be selected independently.
- Import or runtime deprecation signaling must not make pack loading fail. Removal timing belongs to
  compatibility policy.

**Why this way.** Renaming the type corrects the security claim while preserving the stable YAML
shape and prompt content. Inventing enforcement semantics for free-form prose would be unsafe and
would couple casts to tools.

### D2 — `PermissionPolicy` is the executable per-tool decision contract

The target retains the shipped decision shape and makes validation stricter
(`lionagi/agent/permissions.py`):

```python
PermissionBehavior = Literal["allow", "deny", "escalate"]
PermissionMode = Literal["allow_all", "deny_all", "rules"]

@dataclass(frozen=True, slots=True)
class PermissionDecision:
    behavior: PermissionBehavior
    tool_name: str
    action: str
    reason: str
    matched_rule: str | None = None

@dataclass(slots=True)
class PermissionPolicy:
    mode: PermissionMode = "allow_all"
    allow: dict[str, list[str]] = field(default_factory=dict)
    deny: dict[str, list[str]] = field(default_factory=dict)
    escalate: dict[str, list[str]] = field(default_factory=dict)
    on_escalate: EscalationHandler | None = None

    def check(
        self,
        tool_name: str,
        action: str,
        args: Mapping[str, Any],
    ) -> PermissionDecision: ...

    def to_pre_hook(self) -> PreToolHook: ...
```

Rules use case-insensitive glob matching through `fnmatch`. Tool-key aliases are normalized:

| Input key | Canonical key |
|---|---|
| `bash_tool` | `bash` |
| `reader_tool` | `reader` |
| `editor_tool` | `editor` |
| `search_tool` | `search` |
| `context_tool` | `context` |
| any other name | lowercase name |

The evaluated match string is:

| Tool | Match input |
|---|---|
| `bash` | `args["command"]` or empty string |
| `editor` | `args["file_path"]` or empty string |
| `reader` | `args["path"]` or empty string |
| `search` | `pattern + " " + (path or "")` |
| other/MCP | `action` followed by argument values in mapping order |

Rules for the canonical tool and wildcard `"*"` are concatenated. Evaluation order is normative:

```text
mode=allow_all → allow immediately
mode=deny_all  → deny immediately
rules mode:
    reject shell-control operators for bash
    first matching deny rule     → deny
    first matching allow rule    → allow
    first matching escalate rule → escalate
    no match                     → deny
```

The bash shell-control check rejects `;`, `&&`, `||`, `|`, backticks, `$(...)`,
redirection, and newline before glob rules. Even `allow={"bash": ["*"]}` does not override it.

Preset contracts remain:

```python
PermissionPolicy.allow_all()
PermissionPolicy.deny_all()
PermissionPolicy.read_only()
PermissionPolicy.safe()
```

`read_only` allows reader, search, and context; denies editor and bash. `safe` allows reader,
editor, search, and context; denies destructive bash patterns and escalates other bash commands.
Because deny is checked before escalate, a destructive pattern does not reach escalation.

**Escalation semantics.**

- If `on_escalate is None`, escalation raises `PermissionError` before invocation.
- Callback result `True` allows unchanged arguments.
- Callback result `dict` replaces arguments and is then subject to the final security check in D4.
- Any other result denies with `PermissionError`.
- Callback exception denies and propagates as a pre-invocation failure.
- A prompt `escalations` string never supplies this callback.

**Validation changes in the target.**

- Unknown `mode` raises configuration error instead of falling through to rules/default-deny.
- Rule keys and patterns must be strings; rule collections are defensively copied.
- `on_escalate` must be callable or `None`.
- Decision behavior is a closed Literal/enum rather than an unchecked string.
- Invalid configuration fails during ADR-0043 resolution or construction, before any Tool runs.

**Why this way.** The existing deny/allow/escalate order and default deny are conservative and
tested. Tightening construction validation prevents misspelled modes from masquerading as a valid
policy.

### D3 — attach one interceptor plan after all Tools are registered

The target adapter contract is:

```python
PreToolHook = Callable[
    [str, str, dict[str, Any]],
    Awaitable[dict[str, Any] | None],
]
PostToolHook = Callable[
    [str, str, dict[str, Any], Any],
    Awaitable[Any | None],
]
ErrorToolHook = Callable[
    [str, str, dict[str, Any], BaseException],
    Awaitable[ErrorDisposition | None],
]

@dataclass(frozen=True, slots=True)
class Reraise:
    pass

@dataclass(frozen=True, slots=True)
class ReplaceResult:
    value: Any

ErrorDisposition = Reraise | ReplaceResult

@dataclass(frozen=True, slots=True)
class ToolInterceptorPlan:
    tool_name: str
    security_pre: tuple[PreToolHook, ...] = ()
    transforms: tuple[PreToolHook, ...] = ()
    post: tuple[PostToolHook, ...] = ()
    on_error: tuple[ErrorToolHook, ...] = ()

async def apply_tool_interceptors(
    branch: Branch,
    *,
    policy: PermissionPolicy | None,
    hook_handlers: Mapping[str, Sequence[Callable]],
) -> None:
    """Bind one plan to every Tool currently in branch.acts.registry."""
```

Construction order changes to:

```text
create Branch
register ordinary/coding tools
load and register MCP tools under MCPSecurityConfig
snapshot branch.acts.registry
build one per-tool ToolInterceptorPlan
bind each plan exactly once
grant capabilities
return Branch
```

Plan construction recognizes wildcard, canonical, and current `<name>_tool` compatibility keys.
The registered Tool's actual function name is recorded for audit, while built-in aliases are
canonicalized for PermissionPolicy lookup.

Existing Tool pre/postprocessors are not discarded:

- an existing preprocessor becomes the first user transformation between security passes;
- spec/CodingToolkit pre-hooks follow in declaration order;
- an existing postprocessor runs before additional declared post handlers;
- error handlers run in declaration order only for invocation or postprocessing failure.

The binding operation is idempotent. Reapplying the same policy and handler identities leaves one
plan. Attempting to replace an existing plan with a different plan after the Branch has been
returned raises a construction error; runtime policy swaps require an explicit future contract.

**Exact coverage semantics.**

- Every entry in `branch.acts.registry` at binding time is covered, regardless of whether it came
  from ordinary registration, `CodingToolkit`, or MCP discovery.
- A tool registered after binding is rejected by the Role-backed construction surface or must pass
  through the same binding API before becoming visible.
- Empty registry is valid and binds nothing.
- Missing policy means there are no agent security hooks, but user transforms/post/error handlers
  can still bind.
- `deny_all` blocks discovered MCP tools because policy lookup falls back to their canonical
  registered name and global mode.
- CodingToolkit and ordinary Tool no longer maintain separate execution code. They produce handler
  declarations consumed by the same plan.
- Construction does not mutate the input `AgentSpec.hook_handlers`; it snapshots handlers into
  tuples.

**Why this way.** Binding after discovery closes the coverage gap and makes the registry snapshot
the auditable authorization surface. One plan preserves hook ordering without forcing MCP
registration code to know agent policy.

### D4 — invocation follows a fail-closed interceptor state machine

The target execution algorithm is:

```python
async def invoke_with_interceptor(
    tool: Tool,
    plan: ToolInterceptorPlan,
    arguments: dict[str, Any],
) -> Any:
    original = dict(arguments)
    current = dict(original)
    security_changed = False

    def replacement_or_current(value: Any, prior: dict[str, Any]) -> dict[str, Any]:
        if value is None:
            return prior
        if not isinstance(value, dict):
            raise TypeError("interceptor must return dict or None")
        return dict(value)

    # S1: security on caller-supplied arguments
    for check in plan.security_pre:
        prior = current
        replacement = await check(
            plan.tool_name, action(current), dict(current)
        )
        current = replacement_or_current(replacement, current)
        security_changed = security_changed or current != prior

    # T: user/existing transformations
    for transform in plan.transforms:
        replacement = await transform(
            plan.tool_name, action(current), dict(current)
        )
        current = replacement_or_current(replacement, current)

    # S2: security on final arguments after any replacement. A security
    # callback may affirm the mapping by returning None or an equal dict; a
    # second transformation is unstable and fails closed.
    if plan.transforms or security_changed:
        for check in plan.security_pre:
            replacement = await check(
                plan.tool_name, action(current), dict(current)
            )
            checked = replacement_or_current(replacement, current)
            if checked != current:
                raise PermissionError(
                    "security hook changed arguments during final validation"
                )

    # Transformation output must still satisfy the Tool request model and
    # callable schema. Validation is outside the recoverable invocation region.
    current = validate_tool_arguments(tool, current)

    try:
        result = await invoke_tool_callable(tool, current)
        for handler in plan.post:
            replacement = await handler(
                plan.tool_name, action(current), current, result
            )
            if replacement is not None:
                result = replacement
        return result
    except get_cancelled_exc_class():
        raise
    except Exception as exc:
        return await handle_invocation_error(plan, current, exc)
```

The pseudocode shows ordering; cancellation classes continue to follow the runtime's cancellation
policy and are not casually converted to results.

**Security semantics.**

- A security check exception, explicit deny, unresolved escalation, invalid return type, or
  cancellation prevents invocation.
- Security and transformation failures occur outside the recoverable invocation-error region.
  User error handlers cannot turn a PermissionError into a successful result.
- When neither a security check nor a transform replaces arguments, security runs once.
- After any security replacement or when at least one transform exists, the complete security
  chain runs again on the final mapping. A transformer cannot rewrite allowed arguments into
  denied arguments.
- A security callback that returns replacement arguments is itself followed by remaining checks and
  by the final security pass. The final pass may affirm the mapping with `None` or an equal
  dictionary; attempting another replacement is unstable and fails closed.
- Argument mappings are copied at entry; a handler must return a replacement mapping rather than
  relying on mutation.
- Tool request-model/schema validation still occurs before or as part of `FunctionCalling`
  construction; interceptor transformation output is validated again against the callable contract
  before invocation.

**Success and error semantics.**

- Post handlers may replace a successful result. `None` means no replacement.
- If a post handler raises, the error path receives that exception and the underlying tool is not
  invoked again.
- Error handlers are not called for governance or permission denial.
- Each invocation/post error handler receives the same tool name, final arguments, and original
  exception.
- `None` or `Reraise()` keeps the original exception. `ReplaceResult(value)` converts only an
  invocation/post failure into a result.
- If multiple error handlers return replacement results, the first replacement wins and remaining
  handlers are observational only; a later handler cannot restore invocation.
- If an error handler raises, the original invocation exception remains primary and the handler
  exception is attached/logged as secondary.
- Post/error handlers cannot cause a denied callable to run because they execute only after both
  security passes and only around the callable/post region.

This target supersedes the shipped partial behavior where `_chain_post_hooks()` skips every
non-dict result and `error:*` registrations are stored but never invoked.

**Why this way.** Authorization must judge the arguments that actually reach the callable. Keeping
denial outside error recovery makes “fail closed” mechanically true instead of a documentation
promise.

### D5 — MCP transport admission is explicit and fail-closed

The existing transport type already has the required shape
(`lionagi/service/connections/mcp_wrapper.py`):

```python
@dataclass(frozen=True)
class MCPSecurityConfig:
    allow_commands: bool = False
    command_allowlist: frozenset[str] | None = None
    allow_urls: bool = False
    url_allowlist: frozenset[str] | None = None
    env_denylist_patterns: frozenset[str] = <sensitive defaults>
    filter_sensitive_env: bool = True
    max_connections_per_server: int = 5
```

The target construction signatures expose it:

```python
async def create_agent(
    config: AgentSpec,
    *,
    ...
    trust_project_settings: bool = False,
    mcp_security: MCPSecurityConfig | None = None,
) -> Branch: ...

async def _load_mcp(
    branch: Branch,
    spec: AgentSpec,
    *,
    trust_project_settings: bool,
    mcp_security: MCPSecurityConfig,
) -> None: ...
```

`None` resolves to `MCPSecurityConfig()`, whose command and URL transports are denied. An explicit
configuration file path chooses a file; it does not imply `allow_commands=True` or
`allow_urls=True`.

**Exact transport semantics.**

- Project-file discovery still requires `trust_project_settings=True`.
- Command transport additionally requires `allow_commands=True` and, when present, an exact bare
  command allowlist match.
- A command containing a path separator is rejected under allowlist mode; callers allow the bare
  executable name instead.
- URL transport additionally requires `allow_urls=True`, an `https` or `wss` scheme, and, when
  present, an exact hostname allowlist match.
- Sensitive environment variables are removed by default using case-insensitive deny-pattern
  matching.
- The target enforces `max_connections_per_server=5` as the per-server pool cap. The field and
  default already exist, but the shipped pool does not read them, so there is no cap today. Five is
  an inherited value with no recorded sizing rationale; implementation must either enforce it or
  remove the misleading field rather than present it as measured.
- Transport denial aborts that discovery path before tool registration.
- Successfully discovered Tools are still subject to D3/D4 PermissionPolicy interception.
- Permission allow does not relax transport denial, and transport allow does not create a
  PermissionPolicy allow rule.

**Why this way.** Discovery can execute commands or connect to a service before a Tool invocation
exists. It therefore needs its own admission decision. Passing the existing fail-closed type avoids
inventing a second trust vocabulary.

### D6 — Session governance is an earlier executable control

The Session gate contract remains separate (`ToolInvocation` in `lionagi/session/control.py`,
`SessionObserver.authorize` in `lionagi/session/observer.py`, and `lionagi/operations/act/act.py`):

```python
@dataclass(frozen=True, slots=True)
class ToolInvocation:
    function: str
    arguments: dict = field(default_factory=dict)
    branch_id: str | None = None

async def SessionObserver.authorize(action: Any) -> bool: ...
```

Before Tool lookup/invocation, action execution passes a `ToolInvocation` to
`branch.authorize()`.

**Exact semantics.**

- With no Session/observer gate, authorization returns `True`.
- A falsy gate result denies.
- A gate exception also denies.
- Denial stores a `GateDenied` signal and returns a tool-shaped error response so an agent loop can
  adapt; the callable is not invoked.
- A governance allow does not skip PermissionPolicy. Both gates must allow.
- Prompt directives do not participate in either check.
- HookBus `TOOL_PRE` observers occur after the governance gate and do not replace it.
- Transport trust occurs earlier during discovery and is not consulted again as an action gate.

The effective control order is therefore:

```text
transport admission at construction
    AND Session governance at action time
    AND agent PermissionPolicy at Tool time
```

**Why this way.** These controls answer different questions: may this transport be loaded, may this
session attempt this action, and may this resolved agent invoke this Tool with these arguments.
Collapsing them would either grant ambient authority or couple unrelated lifetimes.

## Consequences

- Readers can distinguish model guidance from executable controls by type and package ownership.
- Existing pack YAML remains readable while public naming stops claiming enforcement.
- Built-in, CodingToolkit, and MCP Tools share one security/transform/post/error contract.
- Argument transformers cannot evade policy by changing an already-approved call.
- MCP discovery becomes fail-closed unless the caller explicitly admits its transport.
- Callers that currently rely on automatic command/URL trust or unwrapped MCP tools must configure
  transport and invocation permissions.
- The second security pass adds policy-evaluation cost only when transforms exist. This is accepted
  because argument mutation is exactly the case that invalidates the first result.
- Error recovery becomes explicit and typed; denial is deliberately not recoverable through user
  error hooks.
- Postprocessors that relied on receiving only dictionaries must adapt to the general result type.
- Construction must either reject or bind tools added after the registry snapshot.
- Reversing D1 is low implementation cost but high semantic risk because it restores a false
  security claim.
- Reversing D3/D4 is high cost once all tool families rely on one state machine.
- Reversing fail-closed MCP defaults is localized but expands the construction trust boundary.
- Prompt escalation still has no routing guarantee. Maintainers must not describe it as one until a
  typed router exists.

## Alternatives considered

### Keep `RolePolicy` and improve documentation only

This is source-compatible and avoids catalog migration. It lost because the type name and “runtime
operational envelope” description continue to imply enforcement at every import and API surface.
Documentation would be fighting the public vocabulary.

### Convert free-form directives directly into PermissionPolicy rules

This would make the prompt block executable and unify the two models. It lost because prose such as
“cannot override a security ruling” has no unambiguous tool/action/resource mapping. Parsing it
would create false confidence and unpredictable denial.

### Move prompt directives into the executable governance package

One package could own all concepts named policy. It lost because casts needs prompt composition
without depending on Session or tools. Non-enforcing behavioral text belongs with the Role; only
its name and claims need correction.

### Attach permission hooks while each tool is registered

This is the current simple model and avoids a registry-wide pass. It lost because registration has
multiple sources and MCP discovery happens later. Every new source would need to remember an
agent-specific concern, making missing coverage the default failure mode.

### Apply the interceptor only to MCP Tools

This would close the visible gap with minimal changes. It lost because CodingToolkit, standalone
built-ins, and MCP would still retain separate ordering and error semantics. The requirement is one
auditable invocation contract.

### Run security only before user transformations

This is cheaper and was a common preprocessor shape. It lost on a concrete failure: an allowed
`echo` command can be rewritten by a user hook into denied `rm` arguments. The existing
double-check test demonstrates the need.

### Run security only after transformations

This avoids the double check while judging final arguments. It lost because untrusted transformers
would receive arguments the initial policy might forbid and escalation callbacks could be bypassed.
The first pass is admission to preprocessing; the second is admission to invocation.

### Let error handlers recover permission denials

Uniform error recovery would simplify one handler API. It lost because a user hook could return a
success value after deny and make the enforcement result externally indistinguishable from an
allowed call. Denial must terminate before the recoverable region.

### Treat MCP transport trust as tool permission

One policy could contain server and tool rules. It lost because transport actions occur during
construction before Tool names and invocation arguments exist. It also conflates permission to load
a server with permission to invoke every discovered capability.

### Default MCP transport admission to allow for compatibility

This preserves current explicit-config behavior. It lost because selecting a file is not informed
consent to execute arbitrary commands or connect to arbitrary URLs. Migration requires explicit
settings, which is the intended trust signal.

### Rely only on Session governance

A single gate before action would reduce layers. It lost because standalone Branches may have no
Session gate, Role-specific least authority differs from session-wide governance, and transport
admission is earlier still. Defense layers have distinct owners rather than duplicated semantics.

## Notes

In pari materia resolves “policy” against executable behavior: a type that only renders strings
into a prompt is a directive. Only `PermissionPolicy` and the pre-invocation governance gate may
claim to allow, deny, or escalate a Tool operation.

Source evidence for the current state: `lionagi/casts/pack.py`,
`lionagi/casts/catalog.py`, `lionagi/agent/spec.py`,
`lionagi/agent/permissions.py`, `lionagi/agent/factory.py`,
`lionagi/tools/coding.py`, `lionagi/protocols/action/tool.py`,
`lionagi/protocols/action/function_calling.py`,
`lionagi/protocols/action/manager.py`,
`lionagi/service/connections/mcp_wrapper.py`, `lionagi/session/control.py`,
`lionagi/session/observer.py`, and `lionagi/operations/act/act.py`.
