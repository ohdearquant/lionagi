# ADR-0047: Agent Charter — Enforceable Governance Document

**Status**: Proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0044](ADR-0044-tool-gates.md) (charter constraints bind to gate_ids), [ADR-0051](ADR-0051-tool-registry-allowlists.md) (charter declares the tool allowlist), [ADR-0052](ADR-0052-policy-resolution.md) (charter version is part of the policy bundle), [ADR-0050](ADR-0050-operation-context.md) (active charter version captured in evidence)
**Related**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md) (charter ratification hash follows the same SHA-256 pattern), [ADR-0042](ADR-0042-task-certificate.md) (certificate records active charter version), [ADR-0046](ADR-0046-jit-tool-grant.md) (JIT grant gates appear as charter constraints), [ADR-0048](ADR-0048-agent-segregation-of-duties.md) (SoD constraints are declared in the charter)

## Context

lionagi's `AgentConfig` (`lionagi/agent/config.py`) is the current primary configuration
artifact for an agent. It carries a `permissions` dict, a `hook_handlers` dict, a `tools` list,
and free-form `extra`. An operator can write:

```python
config = AgentConfig(
    name="pr-reviewer",
    tools=["reader", "bash"],
    permissions={"bash.deny": ["git push *", "git commit *"]},
)
config.pre("bash", guard_destructive)
```

This documents intent clearly. What it does not do is enforce that intent uniformly. The
`permissions` dict is read by `create_agent()` and wired into the branch at session start, but
nothing prevents a downstream call from ignoring it. The `guard_destructive` hook fires if wired
correctly, but there is no structural guarantee that a config claiming "deny destructive commands"
has actually registered that hook. The configuration records what the operator *wanted*; it does
not prove what the system *will do*.

Cross-cutting principle #3 — **every constraint must be enforced, not just documented** — names
this gap precisely. Aspirational constraints without enforcement bindings are not governance; they
are intent commentary. An `AgentConfig` that says "deny network access" but has no gate preventing
a network-capable tool from executing is indistinguishable, at audit time, from one that says
nothing about network access at all.

The gap has a second dimension: mutability. `AgentConfig` is a mutable Python dataclass. Nothing
prevents code from altering the `permissions` dict after the config is created but before the
session starts. There is no signatory record, no hash, and no proof that the config that launched
a session matches the config a human approved. When someone asks "was this agent governed by the
right policy?" the only answer today is "we believe so."

### The applicable prior governance research insight

prior research establishes that a Charter is the canonical governance document for a tenant:
every constraint in the Charter must reference either a `gate_id` (a registered enforcement gate)
or a `service_check` (an importable method). If there is no code enforcing a constraint, that
constraint does not belong in the Charter. The Charter is made immutable by a ratification hash
(SHA-256 of all fields) computed at activation time; signatories are accountable for the exact
content they endorsed. Single active Charter per tenant eliminates governance ambiguity: at any
point in time, exactly one Charter is authoritative. In lionagi terms, tenant maps to agent: each
agent has exactly one active `AgentCharter`.

### Why lionagi needs this

Consider a PR reviewer agent deployed inside an organization. Its `AgentConfig` states it must not
write to the main branch and must emit structured evidence for every file read. These are real
constraints with real consequences if violated. Without `AgentCharter`, an operator reading the
config has to trust that every hook was wired correctly, that no code path bypasses the hook
registry, and that the config was not mutated between authoring and deployment. With
`AgentCharter`, activation fails if `gate_id: "guard_branch_protection"` is not found in the
gate registry, and `hook_name: "log_tool_use"` is not importable. The constraint either binds to
code or the charter does not activate. There is no middle state.

## Decision

We introduce `AgentCharter` as the canonical governance binding for a lionagi agent: a frozen,
hash-ratified document where every constraint references either a registered tool gate
(ADR-0044) or an importable hook, and whose version is captured in every Operation Context
(ADR-0050) produced during the agent's sessions.

### 1. `CharterConstraint` — the enforcement-binding requirement

Every constraint in a charter must prove it has code behind it. Pydantic validation on the
`Element` subclass enforces this structurally at construction time, not at activation time.

```python
# lionagi/protocols/governance/charter.py

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger
from lionagi.protocols.generic.pile import Pile

HookPhase = Literal["message_added", "security_pre", "pre", "post", "error"]
ManagerSurface = Literal[
    "MessageManager",
    "ActionManager",
    "iModelManager",
    "DataLogger",
]


class CharterConstraint(Element):
    """An enforceable constraint declared in an AgentCharter.

    CRITICAL: Exactly one of gate_id or hook_name must be set.
    If there is no code enforcing this constraint, it does not belong here.
    Aspirational constraints have no operational value.

    Attributes:
        constraint_id: Human-readable identifier, unique within the charter.
        description: What this constraint requires, in plain language.
        gate_id: References a registered ToolGate (ADR-0044). Mutually
            exclusive with hook_name.
        hook_name: References an existing hook by importable dotted or
            "module:attribute" path (e.g. "lionagi.agent.hooks.log_tool_use").
            Mutually exclusive with gate_id.
        hook_phase: Existing hook phase used when hook_name is set.
        manager_surface: Branch manager surface affected by this constraint.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    constraint_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    gate_id: str | None = None
    hook_name: str | None = None
    hook_phase: HookPhase | None = None
    manager_surface: ManagerSurface = "ActionManager"

    @model_validator(mode="after")
    def validate_enforcement_binding(self) -> CharterConstraint:
        has_gate = self.gate_id is not None
        has_hook = self.hook_name is not None
        if has_gate == has_hook:
            raise ValueError(
                f"CharterConstraint '{self.constraint_id}' must have exactly one of "
                f"gate_id or hook_name, not both and not neither. "
                f"If there is no code enforcing this constraint, it does not belong "
                f"in the charter. (gate_id={self.gate_id!r}, hook_name={self.hook_name!r})"
            )
        if has_hook and self.hook_phase is None:
            raise ValueError(
                f"CharterConstraint '{self.constraint_id}' with hook_name must declare "
                "the existing hook phase it binds to."
            )
        if has_gate and self.hook_phase is not None:
            raise ValueError(
                f"CharterConstraint '{self.constraint_id}' with gate_id must not declare "
                "a hook_phase."
            )
        return self
```

### 2. `AgentCharter` — the governance document

```python
class CharterActivationEvidence(Element):
    """Evidence that a charter constraint resolved, or failed to resolve, to code."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    constraint_id: str
    binding_kind: Literal["gate", "hook"]
    binding_ref: str
    manager_surface: ManagerSurface
    resolved: bool
    reason: str | None = None


class AgentCharter(Element):
    """Tenant-scoped (agent-scoped) governance document.

    An AgentCharter is immutable after ratification. Its ratification_hash
    is SHA-256 of all fields except ratification_hash itself, computed at
    activation time. Signatories are accountable for the exact content they
    endorsed — the hash proves what they ratified.

    Only one charter may be ACTIVE per agent_id at any time. To change
    governance, create a new charter and supersede this one.

    Attributes:
        charter_id: Unique identifier for this charter document.
        agent_id: The agent this charter governs.
        version: Monotonically increasing integer per charter_id. Version 1
            is the initial charter; each supersession increments by one.
        constraints: Pile of constraints. Every constraint references either a
            gate_id or a hook_name — no aspirational rules.
        allowed_tools: Explicit allowlist of tool names this agent may call.
            Declared here; enforced by the Tool Registry (ADR-0051).
        allowed_models: iModel identifiers this agent may use. Constrains
            iModelManager resolution at session start.
        policy_release_version: Binds this charter to a specific policy bundle
            from ADR-0052. Ensures the constraint set was authored against a
            known policy state.
        ratified_at: Unix timestamp of ratification.
        ratified_by: Ordered list of signatory identifiers. Signatories are
            accountable for this exact content.
        ratification_hash: SHA-256 of all fields except this field itself.
            Computed on first activation. Presence signals the charter is
            ACTIVE or SUPERSEDED — never DRAFT.
        active: True when this charter is the currently governing document
            for agent_id. Exactly one active charter per agent_id at any time.
        superseded_by: charter_id of the charter that replaced this one, or
            None if this charter is still active or was never activated.
        activation_evidence: Pile of typed evidence records produced when
            activation resolves gates and hooks.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    charter_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    constraints: Pile[CharterConstraint] = Field(
        default_factory=lambda: Pile(
            item_type={CharterConstraint},
            strict_type=True,
        )
    )
    allowed_tools: tuple[str, ...] = Field(default_factory=tuple)
    allowed_models: tuple[str, ...] = Field(default_factory=tuple)
    policy_release_version: str
    ratified_at: float
    ratified_by: tuple[str, ...] = Field(default_factory=tuple)
    ratification_hash: str = ""
    active: bool = False
    superseded_by: str | None = None
    activation_evidence: Pile[CharterActivationEvidence] = Field(
        default_factory=lambda: Pile(
            item_type={CharterActivationEvidence},
            strict_type=True,
        )
    )

    def verify_hash(self) -> bool:
        """Return True if ratification_hash matches recomputed hash of content."""
        return self.ratification_hash == _compute_charter_hash(self)

    def constraint_by_id(self, constraint_id: str) -> CharterConstraint | None:
        """Look up a single constraint by its constraint_id."""
        for c in self.constraints:
            if c.constraint_id == constraint_id:
                return c
        return None
```

### 3. Charter hash computation

The hash covers every governance-relevant field. It excludes `ratification_hash` itself (the
field being computed) and `active` / `superseded_by` (lifecycle fields that change without
altering what was ratified).

```python
def _compute_charter_hash(charter: AgentCharter) -> str:
    """Compute SHA-256 over all ratification-relevant fields.

    Fields excluded: ratification_hash (computed), active, superseded_by
    (lifecycle metadata that changes without modifying what was ratified),
    activation_evidence, and Element infrastructure fields.
    """
    content: dict[str, Any] = charter.to_dict(mode="db")
    for field_name in (
        "id",
        "created_at",
        "node_metadata",
        "ratification_hash",
        "active",
        "superseded_by",
        "activation_evidence",
    ):
        content.pop(field_name, None)
    serialized = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()
```

### 4. Charter activation — the enforcement-binding validation gate

Activation validates that every constraint in the charter resolves to real code before the
charter becomes operative. If any constraint fails to resolve, activation raises and the charter
remains in DRAFT state. This is the structural enforcement of cross-cutting principle #3.

```python
class CharterActivationError(Exception):
    """Raised when a charter cannot be activated due to unresolvable constraints."""

    def __init__(
        self,
        charter_id: str,
        failures: Pile[CharterActivationEvidence],
    ) -> None:
        self.charter_id = charter_id
        self.failures = failures
        detail = "\n  ".join(
            f"constraint '{e.constraint_id}': {e.reason}" for e in failures
        )
        super().__init__(
            f"Charter '{charter_id}' activation failed — "
            f"{len(failures)} constraint(s) could not be resolved to code:\n  {detail}\n"
            "Remove unresolvable constraints or implement the missing gates/hooks."
        )


def _resolve_hook(hook_name: str) -> Any:
    if ":" in hook_name:
        module_path, attr = hook_name.split(":", 1)
    else:
        module_path, _, attr = hook_name.rpartition(".")
    if not module_path or not attr:
        raise ImportError(f"{hook_name!r} is not an importable hook path")
    return getattr(importlib.import_module(module_path), attr)


def activate_charter(
    charter: AgentCharter,
    gate_registry: dict[str, Any],
    logger: DataLogger | None = None,
) -> AgentCharter:
    """Validate all constraints resolve to code and return an active charter.

    Args:
        charter: The charter to activate. Must be in DRAFT state
            (ratification_hash == "" or charter.active == False).
        gate_registry: Map of gate_id -> ToolGate object (ADR-0044).
            Used to resolve gate_id constraints.
        logger: Existing DataLogger used to record activation evidence.

    Returns:
        A new frozen AgentCharter with active=True, ratification_hash set,
        and activation_evidence populated.

    Raises:
        CharterActivationError: If any constraint's gate_id is absent from
            gate_registry, or any constraint's hook_name is not importable.
        ValueError: If a charter with active=True is passed (already active
            charters are immutable).
    """
    if charter.active:
        raise ValueError(
            f"Charter '{charter.charter_id}' is already active. "
            "To change governance, supersede it with a new charter version."
        )

    evidence = Pile(item_type={CharterActivationEvidence}, strict_type=True)

    for constraint in charter.constraints:
        if constraint.gate_id is not None:
            resolved = constraint.gate_id in gate_registry
            evidence.include(
                CharterActivationEvidence(
                    constraint_id=constraint.constraint_id,
                    binding_kind="gate",
                    binding_ref=constraint.gate_id,
                    manager_surface=constraint.manager_surface,
                    resolved=resolved,
                    reason=(
                        None
                        if resolved
                        else f"gate_id '{constraint.gate_id}' not found in gate registry"
                    ),
                )
            )
        else:
            assert constraint.hook_name is not None  # enforced by CharterConstraint
            try:
                _resolve_hook(constraint.hook_name)
                evidence.include(
                    CharterActivationEvidence(
                        constraint_id=constraint.constraint_id,
                        binding_kind="hook",
                        binding_ref=constraint.hook_name,
                        manager_surface=constraint.manager_surface,
                        resolved=True,
                    )
                )
            except (ImportError, AttributeError) as exc:
                evidence.include(
                    CharterActivationEvidence(
                        constraint_id=constraint.constraint_id,
                        binding_kind="hook",
                        binding_ref=constraint.hook_name,
                        manager_surface=constraint.manager_surface,
                        resolved=False,
                        reason=f"hook_name '{constraint.hook_name}' is not importable — {exc}",
                    )
                )

    failures = Pile(
        collections=[e for e in evidence if not e.resolved],
        item_type={CharterActivationEvidence},
        strict_type=True,
    )

    if failures:
        raise CharterActivationError(charter.charter_id, failures)

    active_draft = charter.model_copy(
        update={
            "active": True,
            "ratification_hash": "__pending__",
            "activation_evidence": evidence,
        },
        deep=True,
    )
    final_hash = _compute_charter_hash(active_draft)
    active_charter = active_draft.model_copy(
        update={"ratification_hash": final_hash},
        deep=True,
    )
    if logger is not None:
        logger.log(
            {
                "event": "agent_charter.activated",
                "charter_id": active_charter.charter_id,
                "version": active_charter.version,
                "ratification_hash": active_charter.ratification_hash,
                "activation_evidence": [
                    e.to_dict(mode="json") for e in active_charter.activation_evidence
                ],
            }
        )
    return active_charter
```

### 5. Lifecycle state machine

```text
DRAFT ──ratify()──► ACTIVE ──supersede(new_id)──► SUPERSEDED
  │                                                     │
  └── edit fields freely                                └── immutable; superseded_by set
      (hash not yet computed)

ACTIVE: active=True, ratification_hash set, superseded_by=None
DRAFT:  active=False, ratification_hash="" (or absent)
SUPERSEDED: active=False, ratification_hash set, superseded_by=<newer_charter_id>
```

Exactly one ACTIVE charter exists per `agent_id` at any time. The charter store enforces this as
a uniqueness constraint: activating a new charter atomically sets the previous ACTIVE charter to
SUPERSEDED and sets `superseded_by` to the new charter's `charter_id`. Both records are retained.

### 6. Integration with `AgentConfig`

`AgentConfig` retains its current role as the session-level configuration surface. At session
start, `create_agent()` resolves the active charter for `config.extra["agent_id"]` or
`config.name` (if a charter store is configured) and attaches it to the branch. The charter's
`allowed_tools` becomes the authoritative tool allowlist, overriding `AgentConfig.tools` when a
charter is present. The charter's
`allowed_models` constrains `iModelManager` model selection.

The active charter's `charter_id` and `version` are written into the Operation Context
(ADR-0050) for every session that runs under it. Every evidence node produced during the session
carries the charter version in its metadata, making post-hoc governance audits unambiguous.

```python
# Sketch — lionagi/agent/factory.py (existing file)

from typing import Any

from lionagi.agent.config import AgentConfig
from lionagi.session.branch import Branch


def _config_agent_id(config: AgentConfig) -> str:
    return str(config.extra.get("agent_id", config.name))


def _validate_active_charter(
    config: AgentConfig,
    charter: AgentCharter | None,
) -> AgentCharter | None:
    if charter is None:
        return None
    if not charter.active:
        raise ValueError(
            f"Cannot create agent under inactive charter '{charter.charter_id}'. "
            "Activate the charter before passing it to create_agent()."
        )
    if not charter.verify_hash():
        raise ValueError(
            f"Charter '{charter.charter_id}' ratification_hash does not match "
            "current content. The charter may have been tampered with."
        )
    if charter.agent_id != _config_agent_id(config):
        raise ValueError(
            f"Charter '{charter.charter_id}' governs {charter.agent_id!r}, "
            f"not AgentConfig {_config_agent_id(config)!r}."
        )
    return charter


def _apply_charter_to_config(config: AgentConfig, charter: AgentCharter) -> None:
    # ActionManager: tools are registered from AgentConfig.tools, so the
    # charter allowlist becomes authoritative before registration.
    config.tools = list(charter.allowed_tools)

    # Existing Tool pre/post/security/error hooks are wired through AgentConfig
    # and attached by create_agent() to Tool.preprocessor/postprocessor.
    for constraint in charter.constraints:
        if constraint.hook_name is None or constraint.hook_phase == "message_added":
            continue
        hook = _resolve_hook(constraint.hook_name)
        config.hook_handlers.setdefault(f"{constraint.hook_phase}:*", []).append(hook)


def _model_ref(model: Any) -> str:
    provider = getattr(model.endpoint.config, "provider", "")
    model_name = getattr(model, "model_name", "")
    return f"{provider}/{model_name}" if provider and model_name else model_name or provider


def _bind_charter_to_branch(branch: Branch, charter: AgentCharter) -> None:
    charter_ref = {
        "charter_id": charter.charter_id,
        "version": charter.version,
        "ratification_hash": charter.ratification_hash,
    }
    branch.metadata["active_charter"] = charter_ref

    # MessageManager/chat: stamp the active charter on every added message.
    def stamp_charter_on_message(message) -> None:
        message.metadata.setdefault("charter", charter_ref)

    branch.on_message_added.append(stamp_charter_on_message)

    # MessageManager hook constraints use the existing on_message_added hook list.
    for constraint in charter.constraints:
        if constraint.hook_phase == "message_added":
            branch.on_message_added.append(_resolve_hook(constraint.hook_name))

    # ActionManager/action: fail closed if a tool escaped the charter allowlist.
    disallowed_tools = set(branch.acts.registry) - set(charter.allowed_tools)
    if disallowed_tools:
        raise PermissionError(
            f"Tools outside active charter allowlist: {sorted(disallowed_tools)}"
        )

    # iModelManager/model: registered model endpoints must be charter-allowed.
    if charter.allowed_models:
        registered_models = {
            ref for model in branch.mdls.registry.values() if (ref := _model_ref(model))
        }
        disallowed_models = registered_models - set(charter.allowed_models)
        if disallowed_models:
            raise PermissionError(
                f"Models outside active charter allowlist: {sorted(disallowed_models)}"
            )

    # DataLogger/audit: durable provenance for the active Branch managers.
    branch._log_manager.log(
        {
            "event": "agent_charter.bound",
            "charter": charter_ref,
            "message_manager": "MessageManager",
            "action_manager": sorted(branch.acts.registry),
            "imodel_manager": sorted(branch.mdls.registry),
        }
    )


async def create_agent(
    config: AgentConfig,
    *,
    charter: AgentCharter | None = None,
    **kwargs: Any,
) -> Branch:
    active_charter = _validate_active_charter(config, charter)
    if active_charter is not None:
        _apply_charter_to_config(config, active_charter)

    ...  # existing create_agent body creates Branch and registers managers/tools

    if active_charter is not None:
        _bind_charter_to_branch(branch, active_charter)
    return branch
```

### 7. Two-Key authorship model

The charter is a boundary between two roles with different responsibilities:

- **Policy author**: declares *what* is required — writes the constraint text, assigns
  `constraint_id`, references the appropriate `gate_id` or `hook_name`. Does not implement code.
- **Gate implementer**: delivers the `ToolGate` or hook that the constraint references. Does not
  decide *which* constraints exist.

Neither role can produce a valid, active charter alone. The policy author's constraints are
rejected at activation if the gate implementer has not delivered the gate. The gate implementer's
gate has no governance standing unless the policy author declares a constraint referencing it.
This is the structural separation of concern that makes constraint coverage verifiable.

### 8. Worked example — PR reviewer agent charter

```python
import time

from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.governance.charter import (
    AgentCharter,
    CharterConstraint,
    activate_charter,
)

# Policy author writes constraints. Gate implementer has already registered
# "guard_branch_protection" in the gate registry and shipped
# "lionagi.agent.hooks.log_tool_use" as an importable Tool post-hook.

reviewer_charter_draft = AgentCharter(
    charter_id="charter:pr-reviewer:v1",
    agent_id="agent:pr-reviewer",
    version=1,
    constraints=Pile(
        collections=[
            CharterConstraint(
                constraint_id="no-write-main",
                description=(
                    "Agent MUST NOT write to the main branch directly. "
                    "All writes must target a feature branch."
                ),
                gate_id="guard_branch_protection",
                manager_surface="ActionManager",
            ),
            CharterConstraint(
                constraint_id="evidence-every-read",
                description=(
                    "Agent MUST emit a structured log entry for every file read, "
                    "recording tool name, path, and success status."
                ),
                hook_name="lionagi.agent.hooks.log_tool_use",
                hook_phase="post",
                manager_surface="DataLogger",
            ),
            CharterConstraint(
                constraint_id="human-approval-merge",
                description=(
                    "Agent MUST request human approval before merging any branch. "
                    "No autonomous merge is permitted."
                ),
                gate_id="gate_jit_required",  # ADR-0046 JIT grant gate
                manager_surface="ActionManager",
            ),
        ],
        item_type={CharterConstraint},
        strict_type=True,
    ),
    allowed_tools=("reader", "bash", "search"),
    allowed_models=("openai/gpt-4.1", "anthropic/claude-sonnet-4-6"),
    policy_release_version="policy-bundle:v2026.05",
    ratified_at=time.time(),
    ratified_by=("ocean@example.com", "security-team@example.com"),
    ratification_hash="",   # computed by activate_charter()
    active=False,
    superseded_by=None,
)

# Activation fails if any gate_id is absent from the registry or any
# hook_name is not importable. Either the code exists or the charter does not activate.
active_charter = activate_charter(reviewer_charter_draft, gate_registry=gate_registry)

# active_charter.active == True
# active_charter.ratification_hash == SHA-256 of all governance fields
# active_charter.verify_hash() == True
# len(active_charter.activation_evidence) == len(active_charter.constraints)
```

An operator upgrading the charter — say, adding a constraint that restricts the bash tool to
read-only commands — creates `version=2`, lists all signatories who must ratify it, calls
`activate_charter()`, and the store atomically supersedes `version=1`. The old charter is
retained with `superseded_by="charter:pr-reviewer:v2"`. Any session that ran under `version=1`
retains its Operation Context referencing `version=1`; its governance provenance is permanent.

## Consequences

**Positive**

- Governance is verifiable: every constraint maps to a `gate_id` or `hook_name`. An auditor can
  trace `charter constraint → gate → gate execution record` without forensic reconstruction.
- No aspirational rules: the charter cannot contain constraints that have no enforcement binding.
  The document reflects actual system behavior at the time it was activated.
- Tamper evidence: `ratification_hash` detects any post-activation modification to the charter
  content. `verify_hash()` is a one-line check at session start.
- Governance provenance: every evidence node produced during a governed session carries the
  charter version. When governance changes, the before/after boundary is unambiguous.
- Signatories are accountable: they endorsed a specific hash, not a mutable document.

**Negative**

- Rigidity by design: adding a new constraint requires both the policy author to draft it and the
  gate implementer to ship the corresponding gate or hook before the charter can activate. There
  is no shortcut. This is intentional.
- Re-ratification burden: any governance change — even correcting a typo in a `description` field
  — requires a new charter version with re-ratification. Operators should batch changes before
  ratifying.
- Upfront investment: a project adopting `AgentCharter` must implement and register gates for
  every constraint it wishes to declare. Projects without gate infrastructure cannot use charters
  until that infrastructure exists.

## Non-Goals

Explicitly out of scope:

- **Charter inheritance or composition**: a charter that extends another charter, or shares
  constraints via a base template, is not supported in this ADR. If needed, address in a
  follow-on ADR after the base model is stable.
- **Runtime charter mutation**: deliberately impossible. A running session cannot modify its own
  charter. Any change requires a new charter version, re-ratification, and explicit supersession.
- **Charter UI or editor surface**: tooling for authoring, viewing, or approving charters is a
  KHive product concern. This ADR specifies the data model and validation contract only.
- **Multi-tenant charter namespacing**: multiple organizations sharing a lionagi deployment each
  having isolated charter stores is a KHive concern. In the open-source framework, charter scope
  is per-agent-id within a single deployment.
- **Charter inheritance hierarchies**: e.g., an "org-level" charter whose constraints propagate
  down to per-agent charters. Out of scope; addressed in policy resolution (ADR-0052) if needed.
- **Automatic constraint generation**: tooling that inspects gate registry and proposes constraints
  automatically is a product concern, not a framework concern.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| `AgentConfig` only (current) | Permissions and hooks document intent but are not structurally bound. A config claiming "deny network access" with no gate behind it is indistinguishable from one that says nothing. Principle #3 violation. |
| Free-form policy documents (YAML/JSON with no code binding) | Human-readable but not machine-verifiable. An auditor cannot confirm enforcement without reading implementation source. Same gap as AgentConfig. |
| Charter without ratification hash | Charters without a hash can be silently modified after signatories approve them. Signatories become accountable for content they did not review. Tamper detection is lost. |
| Advisory constraints (soft vs. hard tier within charter) | Advisory constraints are ignored in practice and have no enforcement value. If a constraint does not need code enforcement, it belongs in documentation, not the charter. prior research reached the same conclusion. |
| Multiple active charters per agent | Introduces ambiguity about which charter governs a given tool call. Policy resolution (ADR-0052) cannot resolve conflicts between two simultaneously active charters scoped to the same agent. |

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — SHA-256 hash-chain pattern; charter hash follows the same construction
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate records the active charter version as part of the proof artifact
- [ADR-0044](ADR-0044-tool-gates.md) — ToolGate registry; charter `gate_id` references resolve against this registry
- [ADR-0046](ADR-0046-jit-tool-grant.md) — JIT grant gates appear as `gate_id` references in charter constraints
- [ADR-0048](ADR-0048-agent-segregation-of-duties.md) — SoD constraints (no self-approval) are declared as charter constraints
- [ADR-0050](ADR-0050-operation-context.md) — Operation Context captures active charter version; evidence nodes carry charter provenance
- [ADR-0051](ADR-0051-tool-registry-allowlists.md) — Charter `allowed_tools` is the governance source for the tool registry allowlist
- [ADR-0052](ADR-0052-policy-resolution.md) — `policy_release_version` binds the charter to a specific policy bundle
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — EvidenceRef; charter version is embedded in evidence metadata
- `lionagi/agent/config.py` — `AgentConfig`; charter augments rather than replaces this
- `lionagi/agent/hooks.py` — built-in hooks (`guard_destructive`, `guard_paths`, `log_tool_use`) referenced as `hook_name` values
- prior governance research `01_design/025-charter/ADR-025-charter.md` — source pattern (constraints must have enforcement binding, ratification hash, single active per tenant)
