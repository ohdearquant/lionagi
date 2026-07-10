# ADR-0041: Agent specification and Branch construction boundary

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: agent-roles
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0078, v0-0100

## Context

LionAGI needs a reusable description of an agent without forcing description-time code to open
files, resolve credentials, construct an API provider, discover remote tools, or allocate a live
conversation. It also needs one place where that description becomes a fully wired `Branch`.
The shipped implementation divides those responsibilities between `AgentSpec` and
`create_agent()`.

This ADR answers six concrete problems.

**P1 — Identity and runtime state have different lifetimes.** A casts `Role` and ordered
`Mode` set can be inspected, validated, serialized in part, and reused without a provider.
A `Branch` owns live message, action, model, logging, capability, observer, memory, and context
state. Treating the declarative input as the live agent would make composition perform I/O and make
reuse dependent on construction order.

**P2 — Role-backed construction has cross-cutting wiring.** Provider selection, effort translation,
trusted settings, the Lion system preamble, built-in tools, MCP discovery, and emission grants must
agree. If every caller performs those steps independently, prompt order and granted capability
sets drift.

**P3 — prompt and emission overrides need three-state semantics.** The prompt has four ordered
sources. Emission selection distinguishes “use the Role declaration,” “replace it,” and “grant
nothing.” Collapsing `None` and an empty tuple would remove a useful denial state.

**P4 — raw prompt workers are legitimate but are not casts agents.** CLI orchestration supports
bare workers and external prompt profiles. They have no `Role` to compose and therefore cannot
implicitly acquire a Role emission contract. Their direct `Branch` construction is an explicit
escape hatch, not a second Role-backed factory.

**P5 — two unrelated facilities currently use “context” language.** The
`AgentSpec.context_management` flag controls the coding toolkit's `context` tool and a one-line
prompt hint. Pre-turn `ContextProvider` registration and execution live on `Branch` and chat/run
operation preparation. Setting the agent flag does not configure, disable, or populate the provider
registry.

**P6 — the current boundary leaks mutation and has incomplete export and interception coverage.**
Settings hooks and permission hooks are appended to the passed `AgentSpec` during construction.
Reusing one instance can therefore duplicate handlers. The YAML writer omits several serializable
fields. MCP tools are discovered after built-in tool interception and do not receive the resolved
permission preprocessor.

The as-built dependency direction is:

```text
casts Pattern/Role/Mode
          │
          ▼
   casts Profile
          │
          ▼
      AgentSpec                 Branch.providers
   (declared intent)        (pre-turn live registry)
          │                         ▲
          ▼                         │
     create_agent ───────────────► Branch
  settings/provider/tools/         live conversation
  prompt/MCP/capability grants
```

| Concern | Decision |
|---|---|
| Declarative agent contract | D1: `AgentSpec` owns Role-backed identity and requested runtime intent, not live state. |
| Materialization boundary | D2: `create_agent()` is the canonical Role-backed path for I/O, Branch allocation, tools, and grants. |
| Prompt and emission composition | D3: Prompt sources are ordered and emission selection keeps its three distinct states. |
| Raw-prompt construction | D4: Direct `Branch` construction is a visible non-casts escape hatch with no implicit Role grants. |
| Pre-turn context | D5: `ContextProviderRegistry` remains Branch/operations state; `context_management` is only a coding-tool switch. |
| Persistence and reuse | D6: The current YAML and construction mutation are recorded as shipped limitations, not treated as intended completeness. |

This ADR does not decide:

- How per-role model, effort, modes, and external-profile precedence should be resolved. That target
  belongs to ADR-0043.
- How prompt directives are renamed or executable permission interception becomes complete. That
  target belongs to ADR-0044.
- The internal ranking, budgeting, or writeback behavior of context providers. This ADR records
  only their ownership boundary because the full mechanism belongs with messages and operations.
- Provider endpoint selection and rate limiting beyond the inputs that the factory consumes.
- Session-level hook ownership. ADR-0047 records those scopes.
- Branch lifecycle after construction, including chat, operate, persistence, and orchestration
  scheduling.

## Decision

### D1 — `AgentSpec` is a declarative Role-backed runtime request

`AgentSpec` contains a casts `Profile` plus requested runtime choices. It is not a provider, tool
registry, capability registry, or conversation.

**The contract** (`lionagi/agent/spec.py`):

```python
@dataclass
class AgentSpec(HooksMixin):
    profile: Profile
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] = ()
    permissions: PermissionPolicy | None = None
    grant_emissions: bool = True
    emits: tuple | None = None
    pack: str | Pack | None = "default"
    lion_system: bool = True
    extra_prompt: str | None = None
    hook_handlers: dict[str, list[Callable]] = field(default_factory=dict)
    cwd: str | None = None
    yolo: bool = False
    mcp_servers: list[str] | None = None
    mcp_config_path: str | None = None
    context_management: bool = True

    @classmethod
    def compose(
        cls,
        role: Any,
        *,
        modes: list[Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
        tools: tuple[str, ...] | list[str] = (),
        permissions: Any = None,
        pack: str | Pack | None = "default",
        grant_emissions: bool = True,
        emits: tuple | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        yolo: bool = False,
    ) -> AgentSpec: ...
```

`compose()` creates `Profile.compose(role, modes=...)`, resolves the permission representation,
copies `tools` to a tuple, stores `system_prompt` as `extra_prompt`, and returns. It does not
load settings, inspect MCP files, instantiate `iModel`, or allocate `Branch`.

Permission inputs accepted at this boundary are:

| Input | Stored result |
|---|---|
| `None` | `None` |
| existing `PermissionPolicy` | same object |
| `dict` | `PermissionPolicy.from_dict(dict)` |
| `"safe"`, `"read_only"`, `"allow_all"`, `"deny_all"` | corresponding preset |
| unknown string | `ValueError` listing valid presets |
| any other type | `TypeError` |

Hook declaration is mutable builder syntax on the spec:

```python
spec.pre(tool_name, handler)       # key "pre:<tool_name>"
spec.post(tool_name, handler)      # key "post:<tool_name>"
spec.on_error(tool_name, handler)  # key "error:<tool_name>"
```

Each method appends in declaration order and returns the same spec for chaining.

**Exact semantics.**

- Empty `tools` means no agent-factory tools are registered. It does not mean “all tools.”
- `pack=None` disables the directives block. `pack="default"` loads the packaged pack and raises
  if that resource cannot be loaded. A non-default string is not a file path here; it resolves to
  no pack and therefore renders no directives. A caller must pass a `Pack` object for a custom
  pack.
- `system_prompt=""` is normalized to `None` by `compose()`.
- `mcp_servers` and `mcp_config_path` are model fields but are not accepted by `compose()`; callers
  set them on the returned spec or construct the dataclass directly.
- `AgentSpec.coding()` fixes Role `implementer`, effort default `"high"`, and tool request
  `("coding",)`. With `secure=True` it appends destructive-command and workspace path guards.
- The spec is mutable. “Declarative” describes its responsibility, not frozen value semantics.

**Why this way.** A value-like request separates inspectable identity from external effects while
remaining Python-native: Role and Mode objects, model classes, permission objects, and callables do
not need a second configuration language. A fully frozen spec was not shipped because hook builder
methods and coding preset adjustment currently mutate fields.

### D2 — `create_agent()` is the Role-backed materialization boundary

Role-backed callers use the asynchronous factory. It is the only shipped construction path that
both composes the casts prompt and grants the resulting Role emission contract.

**The contract** (`lionagi/agent/factory.py`):

```python
async def create_agent(
    config: AgentSpec,
    *,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
    chat_model: Any = None,
    log_config: Any = None,
) -> Branch: ...
```

The materialization sequence is normative as-built behavior:

```text
1. optionally load settings and append trusted hooks to the passed spec
2. accept chat_model, or parse spec.model and construct iModel
3. allocate Branch
4. compose and install the system message
5. append PermissionPolicy as "security_pre:*" on the passed spec
6. register requested built-in/coding tools and attach their hooks
7. discover and register MCP tools
8. grant the resolved emission Operable, when non-empty
9. return Branch
```

Provider construction translates `provider/model` strings through `parse_model_spec()`. Explicit
`chat_model` wins. Otherwise, if `spec.model` is absent, the factory supplies no model keyword and
`Branch` applies its own default. Provider-specific effort and yolo keyword maps are used only when
the provider supports them. CLI providers receive the placeholder API key their subprocess
transport expects; API providers must resolve real credentials.

Settings trust is two-dimensional:

- Global `~/.lionagi/settings.yaml` may load when `load_settings=True`.
- Project settings are included only when `trust_project_settings=True`.
- Python hook imports are restricted to `trusted_hook_modules`; the default set contains only
  `lionagi.agent.hooks`.
- An untrusted configured module raises `PermissionError`. An absent or unresolvable trusted
  function is skipped by the settings resolver.

Settings hook input has this shipped shape (`lionagi/agent/settings.py`):

```yaml
hooks:
  pre|post|on_error:
    <tool-name>:
      - "trusted.module:function"
      - python: "trusted.module:function"
      - command: ["executable", "arg", "{argument_key}"]
```

A single mapping/string may be supplied instead of the per-tool list and is normalized to a
one-item list. Python import paths without `:` and trusted modules/functions that cannot be
imported resolve to no handler. A command must be an argv list of strings; shell strings raise
`ValueError`.

Pre-command hooks send the argument mapping as JSON on stdin. Non-zero exit, subprocess creation
failure, and the 10-second deadline raise `PermissionError`, preventing the Tool. Post-command
hooks send the result mapping and log/swallow subprocess or timeout failure. Both timeout paths
terminate the process group, then wait up to 2 seconds for exit. Ten and two seconds are inherited
implementation values; no recorded rationale establishes those exact thresholds. An `on_error`
command currently builds the post-shaped callable and is appended to `error:<tool>`, but the
shipped Tool execution path never invokes error handlers (ADR-0044 records the target repair).

MCP discovery follows this search contract:

| Input | Behavior |
|---|---|
| existing `spec.mcp_config_path` | load only that path |
| non-existing explicit path | load nothing; do not fall back |
| no explicit path, project trust true | search ancestor `.lionagi/.mcp.json` and `.mcp.json` |
| no project match | try `~/.lionagi/.mcp.json` |
| no file | return without discovery |
| `mcp_servers is None` | discover all configured servers |
| explicit list | load only the named servers |

The current `ActionManager.load_mcp_config()` supplies
`MCPSecurityConfig(allow_commands=True, allow_urls=True)` when no transport policy is passed.
The factory does not expose an `mcp_security` argument. This is current behavior, not the
aspirational trust contract in ADR-0044.

**Error and repetition semantics.**

- A settings parse, explicit untrusted hook, provider-construction, Branch-construction, prompt,
  built-in tool registration, or emission-grant error propagates; the factory does not return a
  partial Branch.
- Per-server MCP discovery failures other than transport denial are logged by `ActionManager` and
  represented as an empty tool list for that server. Transport-policy denial propagates.
- There is no rollback of external work already performed before a later construction failure.
- Constructing twice from the same spec re-applies settings hooks and inserts another permission
  hook. The factory aliases `spec = config`; it does not copy.
- Built-in tool duplicate handling is delegated to `ActionManager.register_tool(update=False)` and
  raises for duplicate function names.
- MCP registration occurs after built-in interception. Newly discovered `Tool` objects keep their
  own default pre/postprocessor fields and do not receive the spec hook chain.

**Why this way.** The factory is the imperative shell around the declarative spec. It centralizes
cross-cutting order without moving provider or filesystem effects into casts. Keeping Branch
allocation here also gives emission grants one auditable construction point.

### D3 — prompt order and emission selection are deterministic

**The prompt contract** (`lionagi/agent/spec.py` and `lionagi/agent/factory.py`):

```text
optional LION_SYSTEM_MESSAGE
Role.body
Mode[0].behaviors
Mode[1].behaviors
...
selected Pack RolePolicy block
extra_prompt
optional coding-context one-liner
```

Within `AgentSpec.build_system_message()`, non-empty parts are joined by exactly two newline
characters. The Role policy block renders sections in this order when their tuples are non-empty:
`## Authority`, `## Operational Boundaries`, `## Escalation Conditions`. The factory then
prepends `LION_SYSTEM_MESSAGE.strip() + "\n\n"` when `lion_system=True`. The coding-context
one-liner is appended before that preamble step when `"coding" in spec.tools` and
`context_management=True`.

An empty composed message installs no system message. An empty casts body can still produce a
message through directives, extra prompt, or the coding one-liner.

**The emission contract**:

```python
def emission_operable(self) -> Operable | None:
    if not self.grant_emissions:
        return None
    if self.emits is not None:
        return build_emission_operable(
            tuple(self.emits),
            name=f"{self.profile.role.name}_emissions",
        )
    return self.profile.emission_operable()
```

| State | Meaning |
|---|---|
| `grant_emissions=False` | grant nothing, regardless of `emits` |
| `grant_emissions=True, emits=None` | use the Role's declaration |
| `grant_emissions=True, emits=(...)` | replace the Role declaration |
| `grant_emissions=True, emits=()` | explicit empty override; grant nothing |

A non-empty emission tuple is converted to an `Operable` and gains
`EscalationRequest` unless already present. The factory calls
`branch.grant_capabilities(op)` only for a truthy non-`None` result.

**Why this way.** Ordered prompt composition makes mode ordering meaningful and reproducible.
Three-state emission selection lets orchestration suppress, inherit, or narrow a Role contract
without editing the Role itself.

### D4 — direct `Branch` construction is a raw-prompt escape hatch

CLI orchestration has two construction families (`lionagi/cli/orchestrate/_orchestration.py`):

```text
built-in casts Role
    AgentSpec.compose(role, modes, extra prompt)
    → create_agent(...)
    → Branch with casts prompt and factory wiring

bare worker or external AgentProfile replacement
    Branch(chat_model=..., system=verbatim_system, ...)
    → no Profile composition
    → no Role emission grant
```

A task-level `system_prompt_override` also selects the verbatim path. External profile prompt text
wins over casts composition in the current CLI. Bare workers receive the fixed bare-worker prompt.
Both branches can later be included in a `Session` and receive session-owned routing, but that does
not retroactively make them Role-backed agents.

**Exact semantics.**

- Direct construction is not prohibited; it is the supported path for identities with no casts
  Role.
- A raw path receives no pack directives, Role body, Mode behaviors, factory settings hooks, agent
  permission hooks, built-in agent tools, MCP discovery, or Role emission grants unless its caller
  wires those separately.
- `grant_spawn` is granted after either construction path by orchestration and is independent of
  Role emissions.
- The distinction is behavioral, not merely a different constructor spelling.

**Why this way.** Forcing verbatim prompts through a fabricated Role would claim typed emissions
and directives the prompt did not declare. Conversely, allowing Role-backed callers to bypass the
factory would duplicate and eventually drift the materialization stack.

### D5 — pre-turn context providers belong to `Branch` and operations

The coding context-tool switch and pre-turn provider registry are separate contracts.

**The provider ownership contract** (`lionagi/session/branch.py` and
`lionagi/protocols/context_providers.py`):

```python
class ContextProvider(Protocol):
    async def provide(
        self,
        branch: Branch,
        instruction: Instruction,
    ) -> str | None: ...

class ContextProviderRegistry:
    def __init__(self, budget: int = 2000): ...
    def register(
        self,
        provider: ContextProvider,
        *,
        priority: int = 0,
        max_tokens: int | None = None,
        name: str | None = None,
    ) -> None: ...

class Branch:
    @property
    def providers(self) -> ContextProviderRegistry: ...
```

`Branch.providers` lazily creates the registry. Chat and run preparation invoke it only when
entries exist and a system message provides a render target. Provider blocks are inserted into the
first-message system-guidance fold for that turn, never into the durable message record, and the
per-turn slot is cleared after request preparation. Provider failure is recorded and skipped.

The default 2,000-token provider budget is inherited from the implementation; no recorded rationale
explains that exact number. It bounds ephemeral injection, not the coding context tool.

By contrast, `AgentSpec.context_management=False` has exactly two factory effects:

1. remove `"context"` from `DEFAULT_CODING_TOOLS` before binding `CodingToolkit`;
2. omit the context-curation prompt one-liner.

It does not touch `Branch._context_providers`, `Branch.providers`, or operation preparation.

**Why this way.** Providers depend on the current Branch, instruction, progression, and per-turn
render slot. They are live operation state. The coding context tool is simply one requested tool
plus user guidance and belongs to agent construction.

### D6 — current YAML is a partial export and construction is not idempotent

The shipped YAML surface is:

```yaml
role: <canonical role name>
modes: [<canonical mode names>]
model: <string-or-null>
effort: <string-or-null>
tools: [<tool requests>]
pack: <string-or-null>
system_prompt: <literal-extra-prompt-or-null>
yolo: <bool>
lion_system: <bool>
context_management: <bool>
cwd: <optional string; omitted when falsy>
```

`from_yaml()` additionally accepts `permissions`, but `to_yaml()` never writes it. The writer also
omits `grant_emissions`, `emits`, `hook_handlers`, `mcp_servers`, and
`mcp_config_path`. A `Pack` object serializes as `pack: null`. Hook callables are intentionally
code-only; the remaining omissions make the method a partial export rather than a full spec
round trip.

The current code does preserve `lion_system=False` and `context_management=False` on read because
it restores those keys explicitly after `compose()` applies its defaults.

Construction mutates two parts of the input:

- `apply_hooks_from_settings()` appends resolved handlers through `HooksMixin`;
- `_apply_permissions()` inserts `policy.to_pre_hook()` into `security_pre:*`.

No marker deduplicates either operation.

**Why this way.** The first version optimized for a convenient, human-readable subset and builder
ergonomics. This ADR does not reclassify those choices as a stable persistence or idempotency
contract; the delta below makes the intended repair explicit.

## Consequences

- Callers can inspect and compose Role identity without allocating a provider or Branch.
- Role-backed construction has one place for prompt, tool, MCP, and emission ordering.
- Raw prompts remain possible without silently inheriting a typed Role contract.
- Maintainers debugging an unexpected prompt must check Profile, pack, extra prompt,
  `lion_system`, and the coding-context condition in that order.
- Maintainers debugging tool authorization must distinguish tools registered before and after MCP
  discovery; current coverage is intentionally recorded as incomplete.
- Reversing D1/D2 would be expensive because casts, CLI orchestration, Branch, provider creation,
  and capability grants would all need a new shared lifecycle owner.
- Reversing D4 is moderate cost but requires a typed external Role loader so raw identities can
  truthfully declare emissions.
- Reversing D5 would couple construction to per-turn Branch state and would blur two existing
  context facilities.
- Construction can leave partial external setup before failure and can accumulate handlers on a
  reused spec. Callers needing repeatable construction should create a fresh spec until the delta
  is closed.
- The factory's current transport trust default for an explicitly selected MCP file is broader than
  its project-file discovery default; ADR-0044 owns the target correction.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Make settings and permission application non-mutating and idempotent; accept when constructing twice from one `AgentSpec` installs each handler exactly once and leaves the spec unchanged. | M | (filled at issue-open time) |
| 2 | Apply the resolved permission/interceptor chain after all tools, including MCP-discovered tools, are registered; accept when `deny_all` blocks an MCP tool call in a regression test. | M | (filled at issue-open time) |
| 3 | Define the `AgentSpec` YAML contract as either a complete serializable round trip or an explicitly named partial export; accept when permissions, emission settings, and MCP fields are preserved or documented as excluded. | M | (filled at issue-open time) |
| 4 | Separate the coding context-tool flag from Branch context-provider terminology; accept when public names and documentation cannot imply that `context_management` enables or disables `ContextProviderRegistry`. | S | (filled at issue-open time) |

## Alternatives considered

### Put provider construction and filesystem discovery inside `AgentSpec.compose()`

This would make one call appear self-contained and could return a ready-to-use object. It lost
because composition would become asynchronous and environment-dependent, Role/Profile tests would
need provider credentials and filesystem fixtures, and merely inspecting intent could allocate live
resources. The shipped pure-description/effectful-factory split keeps those failure domains apart.

### Require every caller to construct `Branch` directly

This would remove one factory abstraction and let specialized callers tune every detail. It lost
because each Role-backed caller would have to reproduce prompt ordering, provider effort mapping,
settings trust, tool attachment, MCP discovery, and emission grants. The existing CLI already shows
why direct construction is appropriate only when no Role contract exists.

### Make `AgentSpec` the live agent façade

The spec could hold a Branch internally and forward chat and tool methods. That would provide one
user-facing object, but it would combine reusable configuration with one mutable conversation
identity. Copy, serialization, restart, and multi-Branch use would become ambiguous, so the shipped
types keep the lifecycle boundary explicit.

### Treat external prompts as synthetic Roles

This would route every worker through the factory and reduce constructor variety. It lost because a
prompt file does not provide typed emissions, artifact defaults, canonical Role identity, or a
validated Mode contract. Fabricating those fields would grant ambient capabilities. ADR-0043
instead requires explicit overlay or replacement disposition.

### Make `context_management` control both coding tools and providers

One switch would be superficially convenient. It lost because providers are registered on a live
Branch and may be useful to chat-only agents with no coding toolkit. Conversely, the coding context
tool may be disabled without disabling externally registered pre-turn knowledge.

### Collapse `emits=None` and `emits=()`

A two-state API would be simpler. It lost because orchestration needs both inheritance from the Role
and an explicit “grant nothing” override. `grant_emissions=False` remains the stronger global
short-circuit.

### Make YAML a pickle-equivalent full snapshot

A complete snapshot could preserve callables and runtime objects. It lost because hook callables,
provider clients, and `Pack` objects are code/runtime state and unsafe or non-portable to deserialize
as data. A complete data contract can include serializable intent while keeping code-only handlers
explicitly excluded.

## Notes

In pari materia resolves the boundary: `AgentSpec` fields are interpreted alongside their actual
factory and operation consumers, not as a claim that every similarly named runtime facility is
agent configuration.

Source anchors: `lionagi/agent/spec.py`, `lionagi/agent/factory.py`,
`lionagi/agent/settings.py`, `lionagi/protocols/action/manager.py`,
`lionagi/session/branch.py`, `lionagi/protocols/context_providers.py`,
`lionagi/operations/chat/_prepare.py`, and
`lionagi/cli/orchestrate/_orchestration.py`.
