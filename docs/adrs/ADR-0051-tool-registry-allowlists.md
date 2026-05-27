# ADR-0051: Tool Registry Allowlists

**Status**: Proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md), [ADR-0043](ADR-0043-governed-tool-declaration.md), [ADR-0044](ADR-0044-tool-gates.md), [ADR-0047](ADR-0047-agent-charter.md)
**Related**: [ADR-0046](ADR-0046-jit-tool-grant.md), [ADR-0049](ADR-0049-log-tier-governance.md), [ADR-0050](ADR-0050-operation-context.md), [ADR-0033](ADR-0033-unified-entity-state-model.md)

---

## Context

lionagi's `PermissionPolicy` (`lionagi/agent/permissions.py`) controls per-tool access via
allowlist, denylist, and escalate rules keyed by tool name and glob pattern. That mechanism works
for its intended purpose — session-local access control — but it has a structural gap: the
allowlist is an in-memory list on `AgentConfig`, assembled at agent creation time, with no
record of who authorized which tool, when, or why. There is no way to answer "who approved
`write_file` for agent X?" without reading the code or configuration that instantiated the agent.
There is no expiry. There is no audit entry when the list changes. Multiple call sites — `AgentConfig.coding()`,
`AgentConfig.research()`, and direct `PermissionPolicy(allow={...})` construction — each define
their own allowlists with no shared registry to enforce consistency.

The concrete failure mode is the N-paths problem: a tool that should require explicit approval
(e.g., a database-write tool in a governed context) can be made available by updating any one of
several independent allowlist paths. Each path has different bypass characteristics. An agent that
reaches a tool through an unlisted path is indistinguishable from one that reached it through an
explicitly governed path. Cross-cutting principle #3 — **every constraint must be enforced, not
just documented** — is violated when the approval record lives only in configuration prose.

Cross-cutting principle #1 (fail-closed) also has no expression at the allowlist level today.
`PermissionPolicy` in `mode="allow_all"` (the default for orchestrators) permits any registered
tool unconditionally. If the allowlist is absent or empty, the policy grants rather than denies.
A centralized registry with a hard gate that checks membership before execution inverts this
default: absence of a registry entry is a deny.

### The applicable prior governance research insight

prior research establishes a centralized `Registry` base class for all "is X in the approved
list?" checks. The key principle is that allowlists are not code — they are configuration entries
with attribution. A registry entry is not "a value in a Python list": it is a record with an
actor, a timestamp, a justification, and a scope. When an entry is no longer valid, it is
superseded (not deleted), so the full approval history is always recoverable. The gate operation
(`verify_in_allowlist`) is the single enforcement point: every governed tool call goes through it,
and it fails closed by default.

Translated to lionagi: `ToolRegistryPolicy` holds `RegistryEntry` objects in a `Pile`;
`verify_in_registry` is installed as a security pre-hook on the existing `Tool.preprocessor`
path used by `ActionManager` ([ADR-0043](ADR-0043-governed-tool-declaration.md)); and
`PermissionPolicy.allow` in library mode is re-expressed as an in-memory policy element so that
the same code path is exercised whether the caller is a library user or a KHive tenant.

### Why lionagi needs this

An agent chartered for read-only research access ([ADR-0047](ADR-0047-agent-charter.md)) must
not be able to call a `write_file` tool even if that tool is registered in the branch. Today,
preventing this requires the session constructor to remember to configure `PermissionPolicy` with
a denylist. If the constructor omits this, nothing in the tool execution path raises an error —
the tool runs, the file is written, and the operation record is indistinguishable from an
explicitly approved write. A registry that is consulted unconditionally by the governed tool
pipeline, and that fails closed on a missing entry, makes this omission structurally impossible.

---

## Decision

Introduce `ToolRegistryPolicy` (append-only, scoped, audited) and `RegistryEntry` (frozen
`Element` with full attribution) as the canonical allowlist mechanism for governed tool access.
The `verify_in_registry` HARD gate ([ADR-0044](ADR-0044-tool-gates.md)) consults the policy before
every governed tool call. In library mode, `PermissionPolicy` constructs an in-memory policy
element; in KHive, the same Element records are backend-persisted.

### 1. Scope enum

```python
from __future__ import annotations

from enum import Enum


class RegistryScope(Enum):
    """Determines which agents and sessions an entry applies to.

    Resolution order (most-specific wins): SESSION > AGENT > PROJECT > GLOBAL.
    Any active entry at any scope level grants access; no entry at any level
    denies access.
    """

    GLOBAL = "global"    # all agents and sessions; system-wide approval
    PROJECT = "project"  # one project (KHive project_id or Studio workspace)
    AGENT = "agent"      # one agent_id within a project
    SESSION = "session"  # one session_id; highest specificity, lowest lifetime
```

### 2. RegistryEntry — frozen, attributed, expirable

```python
import hashlib
import json
from typing import ClassVar
from uuid import UUID

from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


class RegistryEvidence(Element):
    """Evidence record for one registry policy decision or mutation."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    event: str
    entry_id: str | None = None
    tool_name: str | None = None
    actor: str
    reason: str
    payload_hash: str | None = None
    details: dict = Field(default_factory=dict)


class ToolGrant(Element):
    """Runtime grant produced when an active allowlist entry authorizes a call."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    tool_name: str
    entry_id: str
    scope: RegistryScope
    scope_id: str
    granted_for_action: str = ""
    expires_at: float | None = None
    evidence: Pile[RegistryEvidence] = Field(
        default_factory=lambda: Pile(
            item_type=RegistryEvidence,
            strict_type=True,
        )
    )


class RegistryEntry(Element):
    """A single approved entry in a Registry.

    Immutable after creation. Supersession (not deletion) is the only way
    to revoke an entry; the original remains in the append-only pile for audit.
    All mutations emit IMMUTABLE evidence per ADR-0041.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    category: str
    """What kind of resource is being allowlisted.

    Canonical categories: "tool" | "model" | "mcp_endpoint" | "url" | "path_prefix".
    New categories may be added; no category is removed once introduced.
    """

    value: str
    """The resource being approved.

    Exact match only (v1). Examples:
    - category="tool":         "write_file"
    - category="model":        "openai/gpt-4o"
    - category="mcp_endpoint": "mcp://khive-memory/v1"
    - category="url":          "api.github.com"
    - category="path_prefix":  "/workspace/project/"
    """

    scope: RegistryScope
    """Breadth of this approval."""

    scope_id: str
    """Identifier for the scope target.

    For GLOBAL: use "*".
    For PROJECT: the project_id string.
    For AGENT: the agent_id string.
    For SESSION: the session_id string.
    """

    registered_by: str
    """Actor who created this entry (human user, orchestrator agent_id, or system)."""

    registered_at: float
    """Unix timestamp (UTC) of registration."""

    reason: str
    """Required justification. Must be non-empty."""

    expires_at: float | None = None
    """Optional Unix timestamp after which this entry is treated as superseded.

    Expired entries are preserved (not deleted) for audit of historical state.
    The is_active() method accounts for expiry.
    """

    evidence: Pile[RegistryEvidence] = Field(
        default_factory=lambda: Pile(
            item_type=RegistryEvidence,
            strict_type=True,
        )
    )
    """Evidence records (ADR-0041) that support this approval decision."""

    superseded_by: str | None = None
    """entry_id of the replacement entry, if this entry has been superseded."""

    _sensitive_fields: ClassVar[set[str]] = set()

    @property
    def entry_id(self) -> str:
        """Compatibility alias for the Element id."""
        return str(self.id)

    def is_active(self) -> bool:
        """Return True if this entry is currently effective."""
        from datetime import datetime, timezone
        if self.superseded_by is not None:
            return False
        if self.expires_at is not None:
            now = datetime.now(timezone.utc).timestamp()
            if now > self.expires_at:
                return False
        return True

    def payload_hash(self) -> str:
        """SHA-256 of the canonical JSON representation of this entry.

        Used by the evidence emission path (ADR-0041) to produce a content-
        addressed reference to this approval record.
        """
        payload = self.to_dict(mode="db", exclude={"metadata", "evidence"})
        payload.pop("node_metadata", None)
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def with_supersession(self, superseded_by: UUID | str) -> "RegistryEntry":
        """Return an immutable replacement with supersession recorded."""
        return self.model_copy(update={"superseded_by": str(superseded_by)})
```

### 3. Registry — append-only, scoped resolution

```python
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import ConfigDict, Field, PrivateAttr

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger, Log
from lionagi.protocols.generic.pile import Pile


class ToolRegistryPolicy(Element):
    """Append-only, audited allowlist policy mounted on ActionManager/PermissionPolicy.

    All writes produce an IMMUTABLE evidence record (ADR-0041). Reads are O(n) in v1
    (n = active entries per category). Pile supplies managed identity lookup and
    synchronized mutation for library-mode policy state; KHive can persist the same
    Element records behind the same API.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    entries: Pile[RegistryEntry] = Field(
        default_factory=lambda: Pile(
            item_type=RegistryEntry,
            strict_type=True,
        )
    )
    grants: Pile[ToolGrant] = Field(
        default_factory=lambda: Pile(
            item_type=ToolGrant,
            strict_type=True,
        )
    )
    evidence: Pile[RegistryEvidence] = Field(
        default_factory=lambda: Pile(
            item_type=RegistryEvidence,
            strict_type=True,
        )
    )

    _data_logger: DataLogger | None = PrivateAttr(default=None)

    def bind_logger(self, data_logger: DataLogger | None) -> "ToolRegistryPolicy":
        """Bind the Branch DataLogger used to persist registry evidence."""
        self._data_logger = data_logger
        return self

    # ------------------------------------------------------------------ reads

    def _scope_candidates(
        self,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[tuple[RegistryScope, str], ...]:
        """Most-specific to least-specific resolution order."""
        candidates = []
        if session_id:
            candidates.append((RegistryScope.SESSION, session_id))
        if agent_id:
            candidates.append((RegistryScope.AGENT, agent_id))
        if project_id:
            candidates.append((RegistryScope.PROJECT, project_id))
        candidates.append((RegistryScope.GLOBAL, "*"))
        return tuple(candidates)

    def active_entry(
        self,
        category: str,
        value: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> RegistryEntry | None:
        """Return the active entry that grants access, or None."""
        for check_scope, check_id in self._scope_candidates(
            project_id=project_id,
            agent_id=agent_id,
            session_id=session_id,
        ):
            for entry in self.entries:
                if (
                    entry.category == category
                    and entry.value == value
                    and RegistryScope(entry.scope) == check_scope
                    and entry.scope_id in {check_id, "*"}
                    and entry.is_active()
                ):
                    return entry
        return None

    def is_allowed(
        self,
        category: str,
        value: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Return True if an active entry exists for this (category, value) pair.

        Scope resolution: walks from most-specific (SESSION) to least (GLOBAL).
        Any active entry at any scope level → allowed. No active entry at any
        level → denied (fail-closed, cross-cutting principle #1).
        """
        return self.active_entry(
            category,
            value,
            project_id=project_id,
            agent_id=agent_id,
            session_id=session_id,
        ) is not None

    def get_entry(self, entry_id: str) -> RegistryEntry | None:
        """Fetch a specific entry by ID (active or historical)."""
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def history(
        self,
        category: str | None = None,
        value: str | None = None,
    ) -> Pile[RegistryEntry]:
        """Return all entries (active and superseded) matching the filters."""
        result = Pile(item_type=RegistryEntry, strict_type=True)
        for entry in self.entries:
            if category is not None and entry.category != category:
                continue
            if value is not None and entry.value != value:
                continue
            result.include(entry)
        return result

    # ----------------------------------------------------------------- writes

    def add(
        self,
        *,
        category: str,
        value: str,
        scope: RegistryScope,
        scope_id: str,
        registered_by: str,
        reason: str,
        expires_at: float | None = None,
        evidence: Pile[RegistryEvidence] | None = None,
    ) -> RegistryEntry:
        """Append a new entry.

        Raises ValueError if reason is empty; all approvals require justification.
        Emits an IMMUTABLE evidence node (ADR-0041) recording the registration
        event, the actor, and the payload_hash of the new entry.
        """
        if not reason.strip():
            raise ValueError(
                "RegistryEntry.reason must be non-empty; all approvals require justification."
            )
        entry = RegistryEntry(
            category=category,
            value=value,
            scope=scope,
            scope_id=scope_id,
            registered_by=registered_by,
            registered_at=datetime.now(timezone.utc).timestamp(),
            reason=reason,
            expires_at=expires_at,
            evidence=evidence or Pile(item_type=RegistryEvidence, strict_type=True),
        )
        self.entries.include(entry)
        self._record_evidence(
            event="entry_added",
            entry=entry,
            actor=registered_by,
            reason=reason,
        )
        return entry

    def supersede(
        self,
        old_entry_id: str,
        *,
        reason: str,
        superseded_by_actor: str,
        replacement: RegistryEntry | None = None,
    ) -> None:
        """Mark an entry as superseded. The original is preserved for audit.

        If replacement is provided, it must already be present in this registry
        (call add() first, then supersede()). Emits IMMUTABLE evidence for the
        supersession event.

        Does not raise if old_entry_id is already superseded — idempotent.
        """
        for entry in self.entries:
            if entry.entry_id == old_entry_id:
                replacement_id = (
                    replacement.entry_id if replacement is not None else "revoked"
                )
                updated = entry.with_supersession(replacement_id)
                self.entries.update(updated)
                self._record_evidence(
                    event="entry_superseded",
                    entry=updated,
                    actor=superseded_by_actor,
                    reason=reason,
                    details={
                        "superseded_by_actor": superseded_by_actor,
                        "supersession_reason": reason,
                        "replacement_entry_id": replacement_id,
                    },
                )
                return

    def record_grant(
        self,
        *,
        tool_name: str,
        entry: RegistryEntry,
        action: str = "",
    ) -> ToolGrant:
        """Record the effective grant used by one governed tool call."""
        grant = ToolGrant(
            tool_name=tool_name,
            entry_id=entry.entry_id,
            scope=entry.scope,
            scope_id=entry.scope_id,
            granted_for_action=action,
            expires_at=entry.expires_at,
        )
        self.grants.include(grant)
        evidence = self._record_evidence(
            event="grant_issued",
            entry=entry,
            actor="policy",
            reason=f"{tool_name} authorized by registry entry {entry.entry_id}",
            details={"grant_id": str(grant.id), "action": action},
        )
        grant.evidence.include(evidence)
        return grant

    # --------------------------------------------------------- evidence bridge

    def _record_evidence(
        self,
        event: str,
        entry: RegistryEntry,
        *,
        actor: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> RegistryEvidence:
        """Record IMMUTABLE evidence for a registry mutation or grant.

        In library mode, the record is retained in a Pile and also written to the
        bound Branch DataLogger when one is available. In KHive, the backend can
        persist the same Element payload to the immutable evidence store
        (ADR-0041, ADR-0049 IMMUTABLE tier).
        """
        evidence = RegistryEvidence(
            event=event,
            entry_id=entry.entry_id,
            tool_name=entry.value if entry.category == "tool" else None,
            actor=actor,
            reason=reason,
            payload_hash=entry.payload_hash(),
            details=details or {},
        )
        self.evidence.include(evidence)
        entry.evidence.include(evidence)
        try:
            if self._data_logger is not None:
                self._data_logger.log(Log.create(evidence))
        except Exception:  # noqa: BLE001
            # The in-memory evidence Pile remains the source of truth in library mode.
            pass
        return evidence
```

### 4. The `verify_in_registry` gate

`verify_in_registry` is a HARD gate in the sense of [ADR-0044](ADR-0044-tool-gates.md): it raises
`PermissionError` and fails closed if the policy has no active entry. It is registered through the
existing hook path (`AgentConfig.pre(...)` before agent creation, or `Tool.preprocessor` on
`ActionManager`-managed tools after registration).

```python
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence

from lionagi.agent.config import AgentConfig
from lionagi.agent.factory import create_agent
from lionagi.agent.hooks import guard_destructive, guard_paths, log_tool_use
from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.action.tool import Tool

from lionagi.agent.governance.registry import RegistryScope, ToolRegistryPolicy


def make_verify_in_registry_gate(
    policy: ToolRegistryPolicy,
    *,
    project_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
):
    """Factory returning an existing lionagi pre-hook.

    Hook signature matches lionagi.agent.hooks:
        async def hook(tool_name: str, action: str, args: dict) -> dict | None

    The hook raises PermissionError to block and returns None to pass through.
    Any registry read failure is treated as deny (fail-closed).
    """

    async def verify_in_registry(
        tool_name: str,
        action: str,
        args: dict,
    ) -> dict | None:
        try:
            entry = policy.active_entry(
                "tool",
                tool_name,
                project_id=project_id,
                agent_id=agent_id,
                session_id=session_id,
            )
            if entry is None:
                raise PermissionError(f"{tool_name} has no active registry entry")
            policy.record_grant(tool_name=tool_name, entry=entry, action=action)
            return None
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PermissionError(
                f"Registry check failed closed for {tool_name}: {exc}"
            ) from exc

    verify_in_registry.__name__ = "verify_in_registry[tool]"
    return verify_in_registry


Hook = Callable[[str, str, dict], Awaitable[dict | None]]


def _chain_tool_preprocessor(
    tool: Tool,
    *,
    security_hooks: Sequence[Hook],
) -> None:
    """Install registry hooks through Tool.preprocessor without replacing Tool."""
    previous = tool.preprocessor

    async def chained(args: dict, **kwargs) -> dict:
        current = args
        for hook in security_hooks:
            result = await hook(tool.function, current.get("action", ""), current)
            if isinstance(result, dict):
                current = result
        if previous is not None:
            result = previous(current, **kwargs)
            result = await result if inspect.isawaitable(result) else result
            if isinstance(result, dict):
                current = result
        return current

    tool.preprocessor = chained


def install_registry_policy(
    action_manager: ActionManager,
    policy: ToolRegistryPolicy,
    *,
    project_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Mount the allowlist check on existing ActionManager-managed Tool objects.

    ActionManager.registry remains the source of registered tools; the policy only
    supplies the allowlist hook that each Tool runs before func_callable.
    """
    hook = make_verify_in_registry_gate(
        policy,
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    for tool in action_manager.registry.values():
        _chain_tool_preprocessor(tool, security_hooks=[hook])


# Library-mode setup composes registry policy with existing hooks.py handlers.
config = AgentConfig.coding()
policy = ToolRegistryPolicy()
policy.add(
    category="tool",
    value="write_file",
    scope=RegistryScope.PROJECT,
    scope_id="my-project",
    registered_by="ocean",
    reason="Approved for sandbox write operations.",
)

# Before create_agent(): AgentConfig wires hooks through Tool.preprocessor.
config.pre("*", make_verify_in_registry_gate(policy, project_id="my-project"))
config.pre("bash", guard_destructive)
config.pre("editor", guard_paths(allowed_paths=["/workspace/project/"]))
config.post("*", log_tool_use)
branch = await create_agent(config)
policy.bind_logger(branch._log_manager)

# For tools registered after create_agent(), install directly on ActionManager.
install_registry_policy(branch.acts, policy, project_id="my-project")
```

### 5. Canonical categories

| Category | `value` field semantics | Example |
|---|---|---|
| `tool` | tool_id as registered in `ActionManager` | `"write_file"` |
| `model` | `"{provider}/{model_name}"` | `"openai/gpt-4o"` |
| `mcp_endpoint` | MCP server URI (scheme + host) | `"mcp://khive-memory/v1"` |
| `url` | External HTTP host (no path) | `"api.github.com"` |
| `path_prefix` | Filesystem prefix; trailing `/` required | `"/workspace/project/"` |

Categories are extensible. New categories are introduced by adding to this table; no category is
removed once any active entry references it.

### 6. Scope resolution semantics

When `is_allowed()` is called with a `(scope, scope_id)` pair identifying the caller's current
context, resolution walks from SESSION → AGENT → PROJECT → GLOBAL. The first active matching
entry at any scope level grants access. If no active entry exists at any scope level, the call
returns `False` (fail-closed).

This means a GLOBAL entry grants access to all agents and sessions. A PROJECT-scoped entry grants
access to all agents within that project but not globally. A SESSION-scoped entry grants access
only within that session's lifetime — typically used for JIT grants ([ADR-0046](ADR-0046-jit-tool-grant.md)).

### 7. Integration with AgentCharter

[ADR-0047](ADR-0047-agent-charter.md) defines an `AgentCharter` with an `allowed_tools` field.
That field is a **snapshot** derived from the active registry entries at charter ratification time
— it is not a live view. Once a charter is ratified, registry changes do not propagate into it
automatically. To revoke a tool from a running agent, the charter must be superseded (which
terminates the current agent session and initiates a new one with the updated charter). This is
intentional: a charter represents a point-in-time, signed commitment; runtime mutability would
undermine the guarantees [ADR-0042](ADR-0042-task-certificate.md) (Task Certificate) depends on.

### 8. Library mode vs. KHive mode

The `ToolRegistryPolicy` element is usable with zero configuration in library mode. An in-memory
policy is created per `AgentConfig` and backs the allow dimension that previously lived only in
`PermissionPolicy.allow`. No persistence is implied; the policy lifetime is the process lifetime.

In KHive, the registry is backend-persisted. `Registry._emit_registry_evidence` writes to the
IMMUTABLE evidence tier ([ADR-0049](ADR-0049-log-tier-governance.md)). Multi-actor approval
workflows (e.g., two-of-three human approvers before a tool is added to a production registry)
are implemented at the KHive layer by gating the `Registry.add()` call behind an approval flow
— the `Registry` class itself is unaware of this orchestration.

`PermissionPolicy` is not removed. It continues to express deny rules and escalation paths that
have no registry equivalent. The registry governs the allowlist dimension; `PermissionPolicy`
governs the deny and escalate dimensions. Both are consulted on every governed tool call.

---

## Consequences

**Positive**

- Allowlist approvals are auditable: every `add()` call emits an evidence node that records who
  approved what, when, and why. "Who approved `write_file` for agent X?" is always answerable.
- Centralized enforcement eliminates the N-paths problem. There is exactly one code path that
  gates tool execution on registry membership.
- Fail-closed default: a tool with no registry entry in any scope is denied, not permitted. An
  absent allowlist no longer implies permission.
- Expiry is first-class: JIT grants (ADR-0046) expire at session end without requiring explicit
  revocation; the audit record of the expired grant is preserved.
- Library and KHive users share the same `Registry` API; behavioral differences are in the
  persistence and evidence-emission backends, not in the allowlist logic.

**Negative**

- Increased API surface: agents that previously relied on `PermissionPolicy(mode="allow_all")`
  must now populate a registry. Migration requires explicit approval records for every previously
  implicit permission.
- Append-only growth: registries accumulate superseded and expired entries indefinitely. In KHive,
  storage tiering of historical entries is a backend concern; in library mode, old entries are
  garbage-collected with the process.
- Expiry management is a new operational concern: SESSION-scoped entries expire silently. Long-lived
  agents that acquire SESSION-scoped grants must renew them or escalate to AGENT/PROJECT scope.

---

## Non-Goals

Explicitly out of scope for v1:

- **Multi-tenant registry isolation**: KHive's tenant-scoped registry (where Tenant A cannot see
  Tenant B's entries) is a KHive v1 concern. The `Registry` class has no tenant_id; the KHive
  backend adds this as a partitioning key.
- **Wildcard or regex matching**: `value` is exact-match only. `"write_*"` is not a valid value;
  each tool must have an explicit entry. Pattern matching introduces ambiguity in scope resolution
  and is deliberately deferred.
- **Multi-actor approval workflows**: requiring two-of-three human approvers before `Registry.add()`
  completes is a KHive workflow concern, not a library concern. The `Registry` class does not model
  approval state machines.
- **Automatic allowlist propagation across tenants**: a GLOBAL entry in one KHive tenant does not
  propagate to another. Propagation is an explicit KHive federation concern.
- **Dynamic registry mutation by agents**: agents may request that a tool be added to a registry
  (via the JIT grant flow, ADR-0046), but agents do not call `Registry.add()` directly. That
  decision is reserved for humans or orchestrators with explicit approval authority.
- **Performance caching layer**: in-memory caching to reduce O(n) lookup cost is deferred to the
  KHive backend. Library-mode registries are small enough that O(n) is acceptable at v1.

---

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Per-feature allowlists (current) | N-paths problem: each feature implements its own allowlist with different bypass and audit characteristics. No shared invariant that "approved = has registry entry". |
| `PermissionPolicy.allow` dict only | No attribution: the dict holds tool names, not the actor who approved them or when. No expiry. No evidence emission. Answers "is X allowed now" but not "who allowed X". |
| Free-form policy text in `AgentConfig` | Not machine-enforceable. A human-readable description of allowed tools has no gate equivalent. Cross-cutting principle #3 requires enforcement, not documentation. |
| OPA policy engine | Correct for complex policy logic; over-engineered for exact-match membership checks. OPA evaluation overhead on every tool call is unjustified when the check is `value in set`. |
| Separate list per category (tool_allowlist, model_allowlist, ...) | Schema proliferation. A unified `Registry` with a `category` discriminator is simpler, shares the same audit and expiry semantics, and avoids inconsistency between implementations. |

---

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — IMMUTABLE evidence emission on every registry mutation
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate depends on immutable evidence produced by registry mutations
- [ADR-0043](ADR-0043-governed-tool-declaration.md) — existing `Tool` pipeline where `verify_in_registry` is a HARD gate
- [ADR-0044](ADR-0044-tool-gates.md) — `gate_in_registry` as a HARD gate in the three-tier enforcement model
- [ADR-0046](ADR-0046-jit-tool-grant.md) — JIT grants are SESSION-scoped registry entries with `expires_at`
- [ADR-0047](ADR-0047-agent-charter.md) — `allowed_tools` is a snapshot from the registry at charter ratification time
- [ADR-0049](ADR-0049-log-tier-governance.md) — registry mutation events are IMMUTABLE tier log records
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — evidence shape reflected by `RegistryEvidence` records in `RegistryEntry.evidence`
- prior governance research `01_design/031-registry-allowlists/ADR-031-registry-allowlists.md` — source pattern (tenant → scope, role/destination/tool registries → unified category discriminator)
