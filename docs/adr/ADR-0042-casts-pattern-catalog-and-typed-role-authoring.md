# ADR-0042: Casts pattern catalog and typed role authoring

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: agent-roles
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0071, v0-0078

## Context

Casts is LionAGI's built-in vocabulary for named agent behavior. It needs to keep behavioral
identity small, deterministic, and typed while still allowing deployments to tune how a known Role
runs. The shipped design uses Python declarations for behavior and a YAML-backed `Pack` for
configuration and prompt overlays.

This ADR answers five concrete problems.

**P1 — Role identity must carry real Python types.** A Role does not only contain prose. Its
`emits` tuple contains Pydantic model classes that become capability contracts. A data-only role
file would need a second name-to-type resolver and a second failure surface.

**P2 — cognitive overlays are ordered and can be incompatible.** A `Profile` needs exactly one
Role, zero or more Modes, deterministic prompt order, and conflict validation that catches a
declaration from either member of a pair.

**P3 — discovery needs one canonical source.** Listing, loading, the API catalog, and the packaged
default must agree on the supported built-in Role and Mode names. Module stems exist for Python
imports; canonical names exist for public lookup. They are related but not interchangeable.

**P4 — runtime tuning must not redefine behavioral types.** Packs need to select model, effort,
default/allowed Modes, active roster membership, and Role-specific prompt directives. They do not
need to introduce arbitrary Python model classes or replace Role bodies.

**P5 — catalog output is descriptive, not executable state.** Consumers need a plain-data view for
documentation and APIs without gaining a way to mutate the canonical declarations or execute agent
behavior.

The source layout is itself part of the authoring contract:

```text
lionagi/casts/
├── pattern.py              PatternKind, Pattern, Role, Mode, load/list helpers
├── profile.py              one Role + ordered Modes
├── emission.py             Pydantic emission models and Operable builder
├── pack.py                 RolePolicy, RoleConfig, Pack
├── catalog.py              derived plain-data catalog
├── packs/
│   └── default.yaml        packaged overlay for every built-in Role
└── roles/
    ├── <role_module>.py     exactly one canonical ROLE object
    └── modes/
        └── <mode_module>.py exactly one canonical MODE object
```

On 2026-07-09 the discoverable set is 40 Roles and 14 Modes. The packaged default pack contains a
`RoleConfig` and `RolePolicy` entry for all 40 Role names. These counts describe the current
filesystem-derived catalog; they are not a separately versioned manifest.

| Concern | Decision |
|---|---|
| Pattern value model | D1: `Pattern`, `Role`, and `Mode` are frozen, slotted Python declarations with typed behavior fields. |
| Identity composition | D2: `Profile` contains one Role and an ordered, conflict-validated Mode tuple. |
| Built-in discovery | D3: one canonical `ROLE` or `MODE` object per inline module defines the closed built-in catalog. |
| Pack boundary | D4: `Pack` overlays known names with runtime config and prompt directives; it does not define behavior types. |
| Emission and catalog projection | D5: Role model classes build typed Operables; `build_catalog()` returns a derived plain-data projection. |

This ADR does not decide:

- Per-call or per-task precedence among pack config, external profiles, and runtime overrides.
  ADR-0043 owns that target.
- Whether prompt-only `RolePolicy` should retain its name. ADR-0044 owns the rename and
  enforcement distinction.
- Agent materialization, provider selection, tool registration, or capability grant timing.
  ADR-0041 owns construction.
- User-defined Role plugins. The current catalog is closed; an extension registry requires a
  separate decision and typed loading contract.
- The internal fields of every emission model. This ADR fixes how Role declarations refer to those
  model classes and how they become an Operable.

## Decision

### D1 — Pattern declarations are frozen, slotted Python values

**The contract** (`lionagi/casts/pattern.py`):

```python
class PatternKind(Enum):
    OTHER = "other"
    ROLE = "role"
    MODE = "mode"

@dataclass(init=False, frozen=True, slots=True)
class Pattern(Params, Composable):
    name: str
    description: str

    @property
    def kind(self) -> PatternKind:
        return PatternKind.OTHER

@dataclass(init=False, frozen=True, slots=True)
class Mode(Pattern):
    behaviors: str = ""
    conflicts_with: frozenset = field(default_factory=frozenset)

    @property
    def kind(self) -> PatternKind:
        return PatternKind.MODE

@dataclass(init=False, frozen=True, slots=True)
class Role(Pattern):
    body: str = ""
    emits: tuple = ()
    artifact_defaults: dict | None = None

    @property
    def kind(self) -> PatternKind:
        return PatternKind.ROLE
```

`Pattern` uses `Params` with `none_as_sentinel=True` and `empty_as_sentinel=True`. The public
semantic split is:

| Type | Owns | Does not own |
|---|---|---|
| `Pattern` | canonical name and description | runtime or prompt behavior |
| `Role` | prompt body, emission model classes, optional artifact defaults | model/provider choice and permission enforcement |
| `Mode` | cognitive behavior text and names of conflicting Modes | Role identity, tools, or emissions |

A Role module is an ordinary Python module:

```python
from lionagi.casts.emission import DesignSpec, ExecutionPlan
from lionagi.casts.pattern import Role

ROLE = Role(
    name="architect",
    description="...",
    emits=(DesignSpec, ExecutionPlan),
    body="...",
)
```

A Mode module has the parallel shape:

```python
from lionagi.casts.pattern import Mode

MODE = Mode(
    name="adversarial",
    description="...",
    conflicts_with=frozenset(),
    behaviors="...",
)
```

**Exact semantics.**

- Frozen dataclass assignment is rejected after construction. `slots=True` prevents arbitrary
  instance attributes.
- `Role.body=""` and `Mode.behaviors=""` contribute no prompt text.
- `conflicts_with` stores canonical Mode names. The Mode object does not resolve or validate those
  names by itself.
- `Role.emits` holds Python model classes, not strings. Import or class-reference failures happen
  when the declaration module is imported.
- `Role.artifact_defaults=None` means the Role makes no default artifact claim.
- `Role.to_dict()` projects emission classes to their `__name__` strings so the result remains
  JSON-friendly; this projection is not a loader contract.
- Equality and hashing follow the frozen value object's inherited/dataclass behavior; runtime agent
  identity is still represented by `Profile`, not object identity of a mutable service.

**Why this way.** Inline Python keeps body, description, artifact defaults, and real emission types
next to each other. Type import failures are visible to ordinary imports and tests. Frozen values
prevent an agent run from rewriting canonical behavior in place.

### D2 — `Profile` composes one Role with an ordered Mode tuple

**The contract** (`lionagi/casts/profile.py`):

```python
@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    role: Role
    modes: tuple[Mode, ...] = ()

    def __post_init__(self) -> None: ...

    def emission_operable(self) -> Operable | None: ...

    def build_system_message(self) -> str: ...

    @classmethod
    def compose(
        cls,
        role: str | Role,
        *,
        modes: list[str | Mode] | None = None,
        name: str | None = None,
    ) -> Profile: ...

    @classmethod
    def from_yaml(cls, path: str | Path) -> Profile: ...

    def to_yaml(self, path: str | Path) -> None: ...
```

Conflict validation walks Modes in input order. For each new Mode it compares all earlier distinct
entries and rejects the pair when either direction declares the other:

```python
if (
    new.name in earlier.conflicts_with
    or earlier.name in new.conflicts_with
):
    raise ValueError(
        f"Mode conflict: {new.name!r} vs {earlier.name!r}"
    )
```

Prompt composition is exactly:

```python
parts = [role.body] if role.body else []
parts += [mode.behaviors for mode in modes if mode.behaviors]
system_message = "\n\n".join(parts)
```

**Exact semantics.**

- `role` may be a canonical string or an existing `Role` object.
- `modes=None` and `modes=[]` both produce `()`.
- Each string Mode is resolved by `Mode.load()`; objects pass through.
- `name=None` defaults to the Role's canonical name. A custom Profile name does not rename the
  Role.
- Mode order is preserved and is prompt order.
- Conflict declarations are effectively symmetric at validation time even if only one Mode lists
  the other.
- Duplicate Mode names are not explicitly rejected. The `seen` dictionary replaces the earlier
  same-name entry after comparison; a duplicate only fails if the declarations themselves make it
  conflict.
- Empty Role body and empty Mode behaviors can yield an empty system message.
- `emission_operable()` delegates only to the Role; Modes cannot add or remove emission types.
- YAML is symmetric for exactly `{name, role, modes}` using canonical public names. Missing
  `role` raises `KeyError`; unknown Role or Mode names raise `ValueError`; file and YAML errors
  propagate.

**Why this way.** One Role keeps identity singular; ordered Modes preserve intentional cognitive
composition. Checking both conflict directions makes correctness independent of which module
declared the incompatibility. Delegating emissions to the Role prevents a reasoning overlay from
quietly expanding runtime authority.

### D3 — inline modules are the canonical closed built-in catalog

The loading contract is:

```python
class Role:
    @classmethod
    def load(cls, name: str, /) -> Role: ...

class Mode:
    @classmethod
    def load(cls, name: str, /) -> Mode: ...

def list_roles() -> list[str]: ...
def list_modes() -> list[str]: ...
```

A public name maps to an import stem by replacing dashes with underscores. For example,
`postmortem-lead` maps to `postmortem_lead.py` and `visual-spatial` maps to
`visual_spatial.py`. This transform is one-way import plumbing; the underscore stem is not a public
alias.

Discovery scans only direct `*.py` files. It excludes names beginning with underscore and the
literal `TEMPLATE` stem, imports every remaining module, reads `ROLE.name` or `MODE.name`, sorts
the canonical names, and returns a new list.

Current Role names:

```text
analyst, arbitrator, architect, assessor, auditor, commentator, contrarian,
coordinator, critic, curator, deployer, entrepreneur, evaluator, explorer,
facilitator, implementer, innovator, investigator, mentor, migrator, modeler,
negotiator, operator, orchestrator, persona, planner, postmortem-lead,
prototyper, refactorer, researcher, responder, reviewer, scribe, strategist,
suggester, synthesizer, tester, translator, troubleshooter, writer
```

Current Mode names:

```text
adversarial, associative, constraint-solving, empathetic, evidential, fast,
framing, metacognitive, premortem, probabilistic, slow, socratic, systematic,
visual-spatial
```

**Exact semantics.**

- `Role.load("postmortem-lead")` imports `roles.postmortem_lead` and accepts the object only when
  `ROLE.name == "postmortem-lead"`.
- `Role.load("postmortem_lead")` looks for that stem but rejects the object as non-canonical because
  its declared name differs. Module stems are not aliases.
- A missing target module becomes `ValueError("Unknown role/mode ... Available: ...")`.
- A `ModuleNotFoundError` raised by a dependency inside an existing declaration module propagates;
  it is not misreported as an unknown pattern.
- Missing `ROLE`/`MODE` attributes, malformed objects, duplicate canonical names, and import-time
  errors are not normalized into a second registry error model. They surface through import/list
  calls and tests.
- `list_roles()` and `list_modes()` derive live results each call; no static manifest or cache is
  authoritative.
- The built-in set is closed by package contents. Packs do not participate in `load()` or
  `list_*()`.

**Why this way.** Find-by-module gives each behavior a searchable, reviewable file and makes Python
the only declaration interpreter. Canonical-name equality prevents accidental alias creation from
filename normalization.

### D4 — Packs overlay known Role names without extending behavior

**The contract** (`lionagi/casts/pack.py`):

```python
@dataclass(frozen=True, slots=True)
class RolePolicy:
    authority: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    escalations: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class RoleConfig:
    model: str | None = None
    effort: str | None = None
    default_modes: tuple[str, ...] = ()
    modes_allow: tuple[str, ...] = ()
    active: bool = True

@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    policies: dict[str, RolePolicy] = field(default_factory=dict)
    configs: dict[str, RoleConfig] = field(default_factory=dict)

    def policy(self, role: str, /) -> RolePolicy | None:
        return self.policies.get(role)

    def config(self, role: str, /) -> RoleConfig | None:
        return self.configs.get(role)

    @classmethod
    def from_file(cls, path: str | Path, /) -> Pack: ...
```

The YAML input shape is:

```yaml
name: default
roles:
  analyst:
    authority: [...]
    boundaries: [...]
    escalations: [...]
    model: <string-or-null>
    effort: <string-or-null>
    default_modes: [...]
    modes_allow: [...]
    active: true
```

Missing sequences become empty tuples. Missing `active` becomes `True`. Missing `name` becomes the
file stem. Each role entry produces both a `RolePolicy` and a `RoleConfig`, even when all fields
use defaults.

**Exact semantics.**

- `policy()` and `config()` are dictionary lookups and return `None` on a miss.
- Pack parsing does not validate that a role key exists in the built-in catalog.
- Pack parsing does not resolve Mode names. Consumers such as CLI mode resolution perform that
  check.
- Pack parsing does not carry Role `body`, `description`, `emits`, or
  `artifact_defaults`; therefore a pack-only name cannot become a loadable Role.
- `frozen=True` prevents rebinding Pack fields, but the contained dictionaries are ordinary mutable
  dictionaries. Consumers treat Packs as read-only configuration values; deep immutability is not
  enforced by the type.
- YAML read and parse errors propagate from `Path.read_text()` and `yaml.safe_load()`.
- The packaged default currently covers all 40 built-in Roles with both config and policy entries.

**Why this way.** A pack is a deployment/configuration overlay, not a second behavioral type
system. This boundary allows runtime tuning without inventing string-to-model-class resolution or
letting configuration files grant new emission types.

### D5 — Role emissions are typed, and catalog output is a derived projection

A Role turns its emission declaration into an Operable through
`lionagi/casts/emission.py`:

```python
def build_emission_operable(
    emits: tuple[type[BaseModel], ...],
    /,
    *,
    name: str = "emissions",
) -> Operable | None:
    models = tuple(emits)
    if not models:
        return None
    if EscalationRequest not in models:
        models = (*models, EscalationRequest)
    specs = tuple(
        Spec(model, name=field_name_for(model))
        for model in models
    )
    return Operable(specs, name=name)
```

`field_name_for()` converts PascalCase to snake_case and handles acronym runs. The emission base
sets Pydantic `extra="forbid"`, so model output with undeclared fields fails validation.

**Exact emission semantics.**

- Empty or sentinel Role emissions return `None`.
- A non-empty Role contract always includes `EscalationRequest`.
- Already declaring `EscalationRequest` does not duplicate it.
- Model order is retained when building specs; each field key derives from the model class name.
- Duplicate model classes are not explicitly deduplicated by the builder.
- `Profile.emission_operable()` and, by default, `AgentSpec.emission_operable()` delegate to this
  Role contract.

The read-only catalog projection is (`lionagi/casts/catalog.py`):

```python
def build_catalog() -> dict:
    pack = _load_default_pack()
    roles = [_role_entry(Role.load(n), pack) for n in list_roles()]
    modes = [_mode_entry(Mode.load(n)) for n in list_modes()]
    return {"roles": roles, "modes": modes}
```

Its payload shapes are:

```python
{
    "roles": [{
        "name": str,
        "description": str,
        "emits": [{"model": str, "key": str}],
        "body": str,
        "config": {
            "active": bool,
            "model": str | None,
            "effort": str | None,
            "default_modes": list[str],
            "modes_allow": list[str],
            "authority": list[str],
            "boundaries": list[str],
            "escalations": list[str],
        } | None,
    }],
    "modes": [{
        "name": str,
        "description": str,
        "behaviors": str,
        "conflicts_with": list[str],  # sorted
    }],
}
```

The packaged pack loader used by catalog generation catches any exception and returns `None` by
default. In that failure mode Roles and Modes still list, every Role's `config` is `None`, and no
agent behavior executes. Each call returns fresh lists and dictionaries; mutating the returned
payload does not mutate Role, Mode, Profile, or Pack state.

**Why this way.** Direct model classes keep the executable type contract in Python. The catalog is
a projection suitable for APIs and documentation, not a parallel registry or mutation surface.

## Consequences

- Role authors work in one Python module with normal imports and type checking.
- Profile prompt output is deterministic from Role body and ordered Mode behaviors.
- A Mode cannot quietly expand a Role's emission authority.
- The public catalog is easy to serialize and safe to mutate locally because it is rebuilt.
- A broken declaration module can make list/load fail at import time. That is deliberate visibility,
  but it means catalog completeness depends on import tests rather than an independent manifest.
- Counts can change when package files change. Maintainers must update documentation that quotes
  them and keep default-pack coverage tests aligned.
- Packs can contain unknown role or mode strings until a consumer validates them. ADR-0043 makes
  that validation fail before Branch construction.
- Reversing Python-native authoring would require a stable schema for model-class references,
  artifact defaults, body text, and extension loading.
- Reversing the closed catalog would require namespacing, collision rules, trust policy, typed
  emissions, unloading/reload behavior, and pack interaction; adding a search path alone is not
  sufficient.
- Contributors must preserve canonical public names when renaming module stems and must treat dash
  replacement as import plumbing only.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Generate and validate a deterministic built-in catalog index from the Python declarations; accept when role files, mode files, canonical names, and default-pack coverage are checked together without changing the authoring format. | S | #2028 |
| 2 | Correct public documentation that says users extend the catalog through packs; accept when packs are described only as overlays unless a typed external Role loader is implemented. | S | (filled at issue-open time) |

## Alternatives considered

### Author Roles and Modes as YAML or JSON

Data files would make non-Python editing and schema validation straightforward and could support
external search paths. They lost because Role emissions are Python model classes and artifact
defaults are typed structures. A data format would need a trusted import resolver, versioned schema,
collision rules, and error translation without a current user-defined-Role requirement.

### Put every declaration in one Python registry module

One file would make count and uniqueness checks obvious and avoid filesystem discovery. It lost
because 40 Role bodies and 14 Mode behaviors would create a high-conflict monolith, reduce
searchability, and make changes to unrelated behaviors share one review surface. Inline modules
keep declarations isolated while list functions derive the set.

### Let packs define new Role bodies and emissions

This would give users an extension mechanism with no new package. It lost because pack YAML
currently has no safe representation for Python model classes and no namespace/collision contract.
Accepting a pack-only name as a Role would either make emissions untyped or create an ambient code
loader disguised as configuration.

### Maintain a hand-written static manifest as source of truth

A manifest would provide deterministic ordering, counts, and early duplicate detection. It lost as
the canonical authoring source because every addition would require two edits and drift could make
the manifest disagree with modules. The current delta proposes a generated and validated index,
not a second hand-maintained truth.

### Use arbitrary entry points or an extension registry now

An extension registry would support third-party Roles and Modes and avoid modifying the package. It
lost because the present requirement is a closed built-in set. Trust, canonical names, collision
handling, dependency failure, emission imports, and pack overlay precedence must be decided before
external code becomes discoverable.

### Make Modes able to add emission models

A Mode such as “adversarial” could then request specialized output automatically. It lost because a
Mode describes how reasoning is shaped, while a Role declares what the agent may emit. Combining
them would make changing prompt style also change runtime capability grants.

### Return live Role and Mode objects from `build_catalog()`

This would avoid projection code and preserve every method. It lost because API and documentation
consumers need serializable, non-executable data. Live objects would expose model classes and blur
inspection with behavior.

## Notes

Expressio unius applies to the closed catalog: the Role and Mode modules are the supported set, and
pack-only names are not implicit patterns. A typed extension registry can be decided separately if
that requirement appears.

Source anchors: `lionagi/casts/pattern.py`, `lionagi/casts/profile.py`,
`lionagi/casts/emission.py`, `lionagi/casts/pack.py`, `lionagi/casts/catalog.py`,
`lionagi/casts/roles/`, `lionagi/casts/roles/modes/`, and
`lionagi/casts/packs/default.yaml`.
