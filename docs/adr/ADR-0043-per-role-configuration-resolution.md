# ADR-0043: Per-role configuration resolution

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: agent-roles
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0074

## Context

LionAGI currently resolves agent configuration in several places. `Pack` parses per-Role model,
effort, default Mode, allowed Mode, active, and prompt-directive values. CLI orchestration selects
some of those fields. `AgentSpec` independently composes the prompt and loads a pack. External
Markdown `AgentProfile` files provide still another prompt and provider overlay.

This ADR defines the target state. It does not claim the resolver described below is shipped.

Five current failure scenarios require one resolution contract.

**P1 — selected-pack drift.** CLI worker construction reads model, effort, and Modes from the
selected custom `Pack`, but its later `AgentSpec.compose()` call omits `pack=env.pack`. The spec
therefore defaults to the packaged `"default"` pack when rendering directives. One worker can
receive runtime tuning from one pack and prompt guidance from another
(`lionagi/cli/orchestrate/_orchestration.py`; `lionagi/agent/spec.py`).

**P2 — precedence is distributed and partly implicit.** Task model overrides, orchestration
defaults, external profiles, pack configuration, and provider defaults are selected in different
branches of `build_worker_branch()`. A maintainer cannot inspect one value to explain why the
effective model, effort, Modes, prompt, and roster membership were chosen.

**P3 — same-name external profiles silently change identity.** `available_roles()` unions built-in
Role names and external agent-profile names. `resolve_worker_spec(role)` attempts the external
profile before the casts-role construction path. A same-named file can therefore replace the casts
prompt with a verbatim prompt merely by existing on disk.

**P4 — invalid configuration degrades silently.** `resolve_modes()` logs and drops unknown or
disallowed Mode names. An explicit empty list is also treated like “not specified,” so pack defaults
return. The worker still starts with behavior different from the request.

**P5 — roster membership is not enforced.** `RoleConfig.active` is parsed and exposed in
`build_catalog()`, but `available_roles()` lists every built-in Role and planner assignment does not
filter or reject inactive Roles.

A naming collision increases the ambiguity: casts `Profile` is one Role plus ordered Modes; CLI
`AgentProfile` is a Markdown prompt/provider overlay. The target gives the external type and its
disposition explicit names.

The target boundary is a pure core inside the existing effectful construction shell:

```text
filesystem/provider discovery
        │ supplies typed inputs
        ▼
resolve_agent_spec(...)       pure, deterministic, no mutation
        │
        ▼
ResolvedAgentSpec             immutable effective values + provenance
        │
        ▼
Role-backed factory or explicit raw-prompt materializer
        │
        ▼
Branch
```

| Concern | Decision |
|---|---|
| Resolution boundary | D1: one pure function returns one immutable `ResolvedAgentSpec` and performs no discovery or construction. |
| Precedence | D2: model, effort, Modes, tools, permissions, and prompt sources have explicit per-field precedence. |
| External prompts | D3: an external profile declares `overlay` or `replacement`; filename collision never chooses a disposition. |
| Roster | D4: selected-pack `active` controls built-in roster membership and assignment validation. |
| Validation | D5: unknown, disallowed, conflicting, or structurally invalid inputs fail before provider or Branch creation. |
| Pack continuity and explainability | D6: the selected `Pack` and per-field provenance pass unchanged into construction. |

This ADR does not decide:

- How pack prompt fields are renamed. ADR-0044 calls the target type
  `RolePromptDirectives`; the resolver treats current `RolePolicy` as its compatibility input.
- How tool permission checks execute. ADR-0044 owns interception.
- How external Markdown files are discovered, which directories are trusted, or how frontmatter is
  parsed. Discovery produces typed resolver input.
- Provider endpoint creation, credential lookup, or model-specific effort translation. Those occur
  after resolution.
- The exact release count for accepting a legacy profile with no disposition. Compatibility policy
  owns the duration; this ADR fixes the warning and semantic fallback while that window exists.
- User-defined casts Roles. A replacement profile is a raw identity, not an extension to the
  built-in Role catalog.

## Decision

### D1 — resolve configuration through one pure typed function

The target public contract is:

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

class PromptDisposition(str, Enum):
    OVERLAY = "overlay"
    REPLACEMENT = "replacement"

@dataclass(frozen=True, slots=True)
class TaskAgentOverrides:
    model: str | None = None
    effort: str | None = None
    modes: tuple[str, ...] | None = None
    extra_prompt: str | None = None
    tools: tuple[str, ...] | None = None
    permissions: PermissionPolicy | None = None
    yolo: bool | None = None
    bypass: bool | None = None
    fast_mode: bool | None = None
    lion_system: bool | None = None

@dataclass(frozen=True, slots=True)
class ExternalAgentProfileInput:
    name: str
    body: str
    disposition: PromptDisposition | None
    base_role: str | None = None
    model: str | None = None
    effort: str | None = None
    yolo: bool = False
    bypass: bool = False
    fast_mode: bool = False
    lion_system: bool | None = None
    artifact_defaults: Mapping[str, Any] | None = None
    timeout: int | None = None
    resume_on_timeout: bool = False
    enabled: bool = True

@dataclass(frozen=True, slots=True)
class RuntimeAgentDefaults:
    model: str | None
    effort: str | None = None
    tools: tuple[str, ...] = ()
    permissions: PermissionPolicy | None = None
    yolo: bool = False
    bypass: bool = False
    fast_mode: bool = False
    lion_system: bool = True

@dataclass(frozen=True, slots=True)
class RawProfileIdentity:
    name: str

@dataclass(frozen=True, slots=True)
class ExternalProfileRef:
    """Explicit selector for a raw replacement identity.

    String Role names always select the built-in casts catalog; callers use
    this type (or an `external:<name>` surface token parsed into it) when they
    intend an external replacement, including on a name collision.
    """

    name: str

@dataclass(frozen=True, slots=True)
class ResolutionDiagnostic:
    code: Literal["legacy_profile_replacement"]
    message: str

@dataclass(frozen=True, slots=True)
class ResolvedAgentSpec:
    identity: Profile | RawProfileIdentity
    selected_pack: Pack
    active: bool

    model: str
    effort: str | None
    tools: tuple[str, ...]
    permissions: PermissionPolicy | None
    yolo: bool
    bypass: bool
    fast_mode: bool
    lion_system: bool

    prompt_disposition: Literal["casts", "overlay", "replacement"]
    prompt_body: str
    prompt_overlays: tuple[str, ...]
    grant_role_emissions: bool
    artifact_defaults: Mapping[str, Any] | None
    timeout: int | None
    resume_on_timeout: bool

    sources: Mapping[str, str]
    diagnostics: tuple[ResolutionDiagnostic, ...]

def resolve_agent_spec(
    role: str | Role | ExternalProfileRef,
    *,
    requested_modes: Sequence[str | Mode] | None,
    selected_pack: Pack,
    task_overrides: TaskAgentOverrides,
    external_profile: ExternalAgentProfileInput | None,
    runtime_defaults: RuntimeAgentDefaults,
) -> ResolvedAgentSpec: ...
```

The concrete implementation belongs in the agent-configuration boundary, not CLI orchestration.
The resolver may depend on casts and the agent permission type. It must not depend on filesystem
search, environment variables, provider endpoints, `iModel`, `Branch`, `Session`, or CLI parser
objects.

`ResolvedAgentSpec` is an effective snapshot:

- all sequences are tuples;
- `sources` and `artifact_defaults` are exposed as read-only mappings or defensive copies;
- permission rule mappings are defensively copied before exposure;
- no method mutates input `Role`, `Mode`, `Pack`, external profile, task overrides, or defaults;
- equal inputs yield equal resolved values and equal diagnostics.

The selected `Pack` is retained by identity for construction and explanation, but is treated as
read-only. A materializer must not re-run precedence or silently replace it with `"default"`.

**Exact semantics.**

- `selected_pack`, `task_overrides`, and `runtime_defaults` are required typed inputs. Callers
  construct defaults explicitly; ambient process settings are not read.
- `role` as a string is resolved only against the built-in casts catalog. An external replacement
  is selected with `ExternalProfileRef`; command/API surfaces may parse the explicit
  `external:<name>` token into that type before calling the resolver.
- A Role object passes through after its canonical name is checked against selected-pack config.
- `ExternalProfileRef(name)` requires an explicitly supplied external profile with the same name.
  The resolver never searches for it.
- The resolver returns no `Branch`, opens no path, imports no user module, and allocates no provider.
- `model` must resolve to a non-empty string or resolution raises `AgentResolutionError` with code
  `model_unresolved`.
- `effort=None` is valid after all precedence levels; it means the later model/provider default.
- Diagnostics are non-fatal compatibility facts only. Invalid configuration is represented by a
  typed exception, never a diagnostic plus altered behavior.

**Why this way.** A pure snapshot makes configuration explainable and table-testable. The
effectful shell can discover inputs and materialize outputs without owning policy.

### D2 — field precedence is explicit and preserves empty overrides

The target precedence table is normative:

| Concern | Highest to lowest precedence |
|---|---|
| Model | task or CLI override → external profile → selected pack → runtime default |
| Effort | task or CLI override → external profile → selected pack → runtime/model default |
| Modes | explicit task value, including empty → selected-pack defaults → none |
| Tools | explicit task value, including empty → runtime default |
| Permissions | explicit task policy → runtime default |
| Boolean provider flags (`yolo`, `bypass`, `fast_mode`) | explicit task value → external profile true → runtime default |
| Lion system preamble | explicit task value → explicit external profile value → runtime default |
| Prompt | explicit raw replacement, or Role → ordered Modes → selected-pack directives → external overlay → explicit task overlay |
| Roster | active built-in Roles from selected pack, plus explicitly enabled raw replacement profiles |
| Artifact defaults | external profile override → Role declaration → none |
| Timeout/resume | external profile → no deadline/no resume |

For model and effort, “external profile” participates only when a profile was explicitly selected
or supplied by the discovery layer. The resolver never searches by name and never lets a file win
because its name matches a Role.

Mode resolution distinguishes absence from emptiness:

```text
requested_modes is None  → use selected-pack default_modes
requested_modes == ()    → use no Modes; do not restore defaults
requested_modes non-empty → validate exactly that ordered sequence
```

`modes_allow=()` retains its current meaning of unrestricted. A non-empty allowlist constrains only
explicit task Modes and pack defaults alike in the target; an invalid pack default is a pack
configuration error rather than trusted input.

Prompt construction uses exactly two newlines between non-empty blocks. For a casts or overlay
identity:

```text
Role.body
Mode[0].behaviors
...
selected-pack directive block
external overlay body, when present
task extra_prompt, when present
```

The global `LION_SYSTEM_MESSAGE` preamble is not part of `prompt_body`. `lion_system` tells the
materializer whether to prepend it. This keeps resolution deterministic without importing a
process-specific prompt resource.

`prompt_overlays` retains only the non-empty external and task overlay blocks, in that order. It is
empty for casts without overlays and for replacement identities. `prompt_body` remains the complete
rendered result used for explanation and raw replacement; the retained overlay tuple lets the
Role-backed adapter reconstruct the same prompt through `AgentSpec` without duplicating Role,
Mode, or selected-pack text.

Boolean flags use explicit optional task fields so `False` can override an external/profile
`True`. An implementation must not use truthiness such as `task_value or profile_value` for these
fields.

The `sources` mapping records at least:

```python
{
    "identity": "casts:architect" | "external:<name>",
    "model": "task" | "external_profile" | "pack" | "runtime_default",
    "effort": "task" | "external_profile" | "pack" | "runtime_default" | "provider_default",
    "modes": "task" | "pack" | "none",
    "tools": "task" | "runtime_default",
    "permissions": "task" | "runtime_default" | "none",
    "prompt": "casts" | "external_overlay" | "external_replacement",
    "active": "pack" | "external_profile",
}
```

**Miss, empty, and conflict semantics.**

- An unknown Role raises `role_unknown` unless an explicitly supplied, enabled replacement profile
  is selected by `ExternalProfileRef`.
- Missing pack config for a built-in Role raises `role_missing_from_pack`. Falling back to another
  pack is forbidden.
- An empty prompt block is skipped without adding separators.
- Duplicate Mode names raise `mode_duplicate`; the target is stricter than current `Profile`
  construction so effective configuration has one unambiguous occurrence.
- A Mode pair conflict raises `mode_conflict` and names both Modes.
- A task extra prompt of `""` is an explicit empty overlay and has no rendered effect; it does not
  select replacement semantics.
- Empty tools means register no configured tools.
- `permissions=None` after precedence means no agent `PermissionPolicy`; the separate governance
  gate may still apply.

**Why this way.** Per-field precedence reflects the current strongest-source intent while making
absence and explicit denial distinguishable. Provenance turns “why did this worker run this way?”
into data rather than source archaeology.

### D3 — external prompt profiles declare overlay or replacement

The external profile input has two legal dispositions.

#### Overlay

`disposition="overlay"` requires `base_role`. The resolver:

1. loads that built-in Role;
2. verifies the requested/selected Role agrees with `base_role`;
3. resolves and validates Modes;
4. renders Role, Modes, selected-pack directives, external body, then task overlay;
5. retains `Profile` identity and Role emission eligibility.

An overlay may override model, effort, runtime flags, artifact defaults, timeout, and resume
settings. It does not redefine the Role's `emits` tuple or Mode catalog.

#### Replacement

`disposition="replacement"` requires `base_role is None`. The resolver:

1. creates `RawProfileIdentity(name)`;
2. rejects non-empty requested Modes with `modes_on_replacement`;
3. uses the external body, followed only by an explicit task prompt overlay;
4. sets `grant_role_emissions=False`;
5. does not render pack Role directives or claim casts Role identity.

A replacement remains a supported raw-prompt escape hatch. It is not added to `Role.load()`,
`list_roles()`, or the casts catalog.

**Collision semantics.**

- A same-named external profile never shadows a built-in Role.
- Supplying a replacement is an explicit caller choice and must use `ExternalProfileRef`; name
  equality alone is insufficient. A same-named built-in Role remains selected by its bare string.
- An `ExternalProfileRef` with no supplied profile raises `external_profile_missing`; a supplied
  profile with a different name raises `external_profile_name_mismatch`.
- An `ExternalProfileRef` paired with `disposition="overlay"` raises
  `external_ref_requires_replacement`. A replacement profile supplied alongside a bare built-in
  Role raises `replacement_requires_external_ref`; it is never silently ignored or applied.
- An overlay whose `base_role` is missing raises `overlay_role_missing`.
- An overlay supplied for a different requested Role raises `overlay_role_mismatch`.
- A replacement with a `base_role` raises `replacement_has_base_role`.
- A disabled external profile raises `external_profile_inactive` when explicitly selected and is
  omitted from roster output.

**Legacy compatibility.**

During the bounded compatibility window, a profile with `disposition=None` is interpreted as
`replacement` and returns:

```python
ResolutionDiagnostic(
    code="legacy_profile_replacement",
    message=(
        "External profile '<name>' has no disposition; treating it as "
        "'replacement' during the compatibility window."
    ),
)
```

The warning is emitted by the effectful caller once per loaded profile, not by the pure resolver
through logging. When the compatibility window closes, the same input raises
`profile_disposition_required`. The number of releases is deliberately not fixed here.

**Why this way.** Overlay and replacement are both valid, but they have different identity and
capability consequences. Making the disposition data prevents filesystem presence from changing
Role semantics.

### D4 — selected-pack active state controls the planner roster

For each built-in Role, selected-pack `RoleConfig.active` is authoritative.

Target roster construction is:

```python
def resolved_role_roster(
    *,
    selected_pack: Pack,
    external_profiles: Sequence[ExternalAgentProfileInput],
) -> tuple[str, ...]:
    """Active built-in Role names plus explicit external:<name> replacement tokens."""
```

**Exact semantics.**

- A built-in Role is listed only when its selected-pack config exists and `active is True`.
- An inactive Role remains loadable for catalog inspection but cannot be assigned through the
  resolved orchestration surface.
- Explicit assignment to an inactive built-in Role raises `role_inactive` before provider creation.
- Overlay profiles do not add a second roster identity; they decorate their base Role when
  explicitly selected.
- Enabled replacement profiles add `external:<name>` tokens. External identities are always
  namespaced this way, not only on collision; a parser converts that token to `ExternalProfileRef`.
- Bare names remain reserved for built-in Roles, so a replacement name colliding with a built-in
  Role is still independently addressable without ambiguity.
- Duplicate external replacement names after discovery are a discovery/configuration error; the
  resolver does not choose search-order winners.
- Planner guidance and assignment validation consume the same tuple. Separate roster formatting may
  not recalculate membership.

**Why this way.** `active` otherwise has no runtime meaning. One roster value prevents the planner
from advertising identities that execution later rejects or, worse, quietly changes.

### D5 — invalid configuration fails before materialization

The target exception contract is:

```python
class AgentResolutionError(ValueError):
    code: Literal[
        "model_unresolved",
        "role_unknown",
        "role_missing_from_pack",
        "role_inactive",
        "mode_unknown",
        "mode_disallowed",
        "mode_duplicate",
        "mode_conflict",
        "pack_default_invalid",
        "overlay_role_missing",
        "overlay_role_mismatch",
        "replacement_has_base_role",
        "modes_on_replacement",
        "external_profile_missing",
        "external_profile_name_mismatch",
        "external_ref_requires_replacement",
        "replacement_requires_external_ref",
        "external_profile_inactive",
        "profile_disposition_required",
        "resume_without_timeout",
    ]
    subject: str | None
    details: Mapping[str, Any]
```

All errors are raised before `iModel`, `Branch`, tool, MCP, or capability allocation.

Validation order is deterministic:

```text
1. classify explicit casts vs external identity
2. validate profile disposition and base-role relationship
3. require selected-pack entry and active state for casts identity
4. resolve model and effort sources
5. choose requested or default Modes
6. validate Mode existence, uniqueness, allowlist, and pair conflicts
7. resolve tools, permissions, flags, artifacts, and deadline values
8. compose prompt and source map
9. return ResolvedAgentSpec
```

The first failure in that order is returned. Bulk diagnostic accumulation is not used for fatal
errors because later checks may depend on an identity or pack entry that did not resolve.

Specific behavior:

- Unknown explicit Modes raise `mode_unknown`, not warnings.
- Disallowed explicit Modes raise `mode_disallowed` and retain the rejected name and allowlist in
  `details`.
- Unknown or disallowed pack defaults raise `pack_default_invalid`, identifying the pack and Role.
- External timeout accepts only a positive non-boolean integer; invalid values are discovery/schema
  errors before resolver invocation.
- `resume_on_timeout=True` with `timeout=None` raises `resume_without_timeout`; auto-resume cannot
  have meaning without a deadline.
- A prompt replacement never receives a Role emission contract even if its name equals a Role.

**Why this way.** Silent dropping changes requested behavior while returning a seemingly valid
agent. Early typed failure is cheaper than debugging a worker that started under unintended Modes
or directives.

### D6 — construction consumes the selected pack and resolved values unchanged

The Role-backed materializer converts the snapshot without re-resolving it:

```python
def agent_spec_from_resolved(resolved: ResolvedAgentSpec) -> AgentSpec:
    """Pure adapter for casts/overlay identities; rejects replacement identity."""

async def create_resolved_agent(
    resolved: ResolvedAgentSpec,
    *,
    chat_model=None,
    log_config=None,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
) -> Branch:
    """Materialize casts/overlay through create_agent; replacement through
    the explicit raw-prompt path."""
```

For casts and overlay identities, the adapter must preserve:

- the resolved `Profile` and ordered Modes;
- the exact `selected_pack` object;
- resolved model, effort, tools, permission policy, `yolo`, `lion_system`, and the exact
  `prompt_overlays` sequence;
- `grant_emissions=resolved.grant_role_emissions`.

The adapter sets `pack=resolved.selected_pack` and joins `prompt_overlays` with exactly two
newlines into `AgentSpec.extra_prompt`. Because the resolved `Profile` already contains the Role
and ordered Modes, `AgentSpec.build_system_message()` then reconstructs `prompt_body` exactly once:
Profile body, selected-pack directives, then the retained overlays. An adapter test must assert that
the reconstructed text equals `resolved.prompt_body` before the global Lion preamble is applied.

For replacement identity it installs `prompt_body` verbatim according to `lion_system` and does
not synthesize a Role, pack directives, or Role grants.

`bypass`, `fast_mode`, `artifact_defaults`, `timeout`, and `resume_on_timeout` remain on
`ResolvedAgentSpec`; they are provider/orchestration inputs, not current `AgentSpec` fields. The
materializer consumes them when it builds the provider and run envelope. If a caller supplies an
already-built `chat_model`, that caller owns provider-only flag application; resolution is not run
again.

Construction may add runtime-only state such as settings hooks, an existing `chat_model`, log
configuration, MCP discovery, and Branch name. It may not repeat precedence, consult a different
pack, restore default Modes after an explicit empty tuple, or search again for a same-named
external profile.

**Why this way.** Resolution is useful only if materialization cannot reinterpret it. Passing the
pack and provenance through makes configuration drift observable and prevents the custom-pack bug
that motivated this ADR.

## Consequences

- Library and CLI callers can explain effective configuration from one immutable value.
- Custom-pack runtime tuning and prompt directives cannot diverge.
- Same-named files stop silently changing casts identity.
- Planner roster and assignment validation share one active-set contract.
- Explicit empty Modes and tools become meaningful denial/disable states.
- Previously tolerated invalid Modes and pack defaults become startup errors. This is an intentional
  behavior change and requires error handling at CLI/API boundaries.
- External profile frontmatter gains a required disposition after a compatibility window.
- The resolver adds public types, error codes, and provenance that must remain stable or be migrated.
- Provider discovery still occurs separately; callers must construct typed inputs before resolution.
- Reversing the pure resolver is moderate-to-high cost once library and CLI surfaces depend on the
  error and source maps.
- Reversing explicit profile disposition would reintroduce hidden identity changes and potential
  capability over-grant.
- A frozen top-level result does not make referenced model classes or Pack internals magically
  immutable; adapters must keep defensive-copy and no-mutation rules.

## Alternatives considered

### Keep today's profile-first lookup

This preserves compatibility and requires no new disposition field. It lost because adding a file
can replace a built-in Role's body, directives, Modes, and emission eligibility without an explicit
request. The behavior is not explainable from the task assignment alone.

### Remove external raw profiles

A casts-only system would have one identity model and one factory path. It lost because verbatim
prompts are a valid escape hatch for specialized workers and existing profiles carry useful model
and timeout defaults. The decision makes replacement explicit instead of removing it.

### Treat every external profile as an overlay

This would keep all workers Role-backed and preserve factory wiring. It lost because some profiles
are intentionally complete prompts with no truthful base Role or emission declaration. Forcing a
base Role would add prompt text and capabilities the author did not request.

### Treat every external profile as a replacement

This matches legacy behavior and is simple. It lost because users also need to tune or extend a
known Role while retaining its typed identity, Modes, directives, and emissions. Overlay expresses
that intent without shadowing.

### Resolve configuration inside `create_agent()`

The factory already owns construction and could read pack, task, profile, and runtime settings.
It lost because resolution would inherit filesystem/provider effects and become hard to test or
reuse. It would also leave CLI and library callers unable to inspect the effective values before
allocation.

### Add a second settings hierarchy outside Pack

A separate agent defaults file could own model/effort precedence and leave Pack prompt-only. It lost
because `RoleConfig` already owns those per-Role fields. A second hierarchy would require
cross-file precedence and could drift from the directives selected for the same Role.

### Keep warning-and-drop Mode validation

This maximizes run completion when planner output is imperfect. It lost because the worker then runs
with different reasoning behavior from the request, and pack typos can persist unnoticed. A typed
configuration error lets the planner or caller correct the assignment before side effects.

### Make `active=False` advisory planner text only

This would avoid hard failures for direct assignments. It lost because an advisory flag still
allows stale or malicious assignments and gives catalog state no enforceable meaning. The same
resolved roster must drive both guidance and validation.

### Store only effective values, not provenance

A smaller result type would be sufficient for construction. It lost because the core motivation is
distributed precedence. Without sources, maintainers still cannot explain why a value won or detect
that selected-pack policy drifted.

## Notes

Contra proferentem resolves ambiguous external profiles toward the minimal explicit contract:
composition and replacement are both supported, but the caller chooses. During migration only, a
missing disposition retains legacy replacement behavior with a typed warning.

Source evidence for the current gaps: `lionagi/casts/pack.py`,
`lionagi/casts/catalog.py`, `lionagi/agent/spec.py`,
`lionagi/cli/_providers.py`, and
`lionagi/cli/orchestrate/_orchestration.py`.
