# ADR-0069: Tenant Scope Boundary

**Status**: accepted
**Date**: 2026-05-27
**Decision owners**: @governance-maintainers
**Supersedes**: none
**Superseded by**: none
**Depends on**: [ADR-0050](ADR-0050-operation-context.md) (OperationContext provides the
per-operation context for policy evaluation; does not carry tenant_id directly),
[ADR-0052](ADR-0052-policy-resolution.md) (PolicyResolver accepts tenant_id as an input
parameter; ScopeLevel and PolicyResolutionResult define the hierarchy and record resolved tenant)
**Related**: [ADR-0047](ADR-0047-agent-charter.md) (charter is the session-binding vehicle),
[ADR-0051](ADR-0051-tool-registry-allowlists.md) (registry entries are policy-scoped),
[ADR-0044](ADR-0044-tool-gates.md) (gates selected by active policy)

---

## Context

Multi-tenant AI agent deployments need different governance policies, tool allowlists, cost
budgets, and audit segregation per organization. The natural implementation question is: how much
of tenant infrastructure does lionagi provide?

Full tenant isolation — isolated data stores, per-tenant network routing, billing metering, tenant
lifecycle management, and cross-tenant access control enforcement — is complex infrastructure.
lionagi does not provide these capabilities. This ADR codifies the precise boundary: what lionagi
provides (hook points and a named scope label) and what it explicitly does not provide (isolation
infrastructure).

The resolution hierarchy introduced in ADR-0052 (Policy Resolution) has exactly four specificity
levels:

```text
resource (3) > role (2) > tenant (1) > global (0)
```

The `TENANT` position at level 1 is a deliberate API commitment: there is a named slot in the
resolution hierarchy reserved for tenant-scoped policy rules. Integrations can write rules at that
level without touching the core resolution engine.

However, the mere existence of a `TENANT` scope label could mislead readers into believing that
lionagi implements tenant isolation. It does not. This ADR codifies the distinction.

### What deployments serving multiple organizations need

When a governed deployment serves multiple organizations, each organization typically requires:

1. **Policy segregation** — different tool allowlists, gate configurations, and permission rules.
2. **Audit segregation** — evidence chains that cannot mix across organizational boundaries.
3. **Storage isolation** — separate data stores or at minimum separate namespaces with enforced
   access control.
4. **Billing metering** — per-tenant cost accounting.
5. **Lifecycle management** — provisioning, de-provisioning, and suspension of tenant contexts.

Items 2–5 are infrastructure concerns. lionagi's governance system addresses item 1 through the
scope hierarchy: a policy rule scoped to `tenant` applies only when the operation's `tenant_id`
matches the rule's matching criteria. That is the entirety of lionagi's tenant support. Items
2–5 require additional infrastructure outside the scope of this library.

### Why the label exists at all

The `TENANT` scope level exists for two reasons:

**Reason 1: Future-proof API surface.** Integrations that need to set policy rules applying
organization-wide (more specific than global defaults, less specific than per-role rules) need
a named slot in the hierarchy. Reserving that slot in the schema allows tenant-scoped charter
rules to be written today without waiting for a protocol revision.

**Reason 2: Interoperability surface.** A tenant middleware that resolves the active tenant for
an incoming request needs a standard field to write the result into. That field is `tenant_id`
on `PolicyResolver.resolve()` and, transitively, on `OperationContext`. Making that field part
of the interface ensures integrations can interoperate without forking the resolution engine.

Neither reason requires lionagi to implement tenant storage, routing, or lifecycle management.

---

## Decision

`TENANT` occupies position 1 in `ScopeLevel` (defined in
`lionagi/protocols/governance/resolution.py`). This is a **permanent API commitment**. The
integer assignment `TENANT = 1` must not change without a superseding ADR.

### What lionagi provides

1. **`ScopeLevel.TENANT` (value 1)** — a named specificity level between `GLOBAL` (0) and
   `ROLE` (2) in the `ScopeLevel` IntEnum. The docstring on `ScopeLevel` explicitly states that
   lionagi does not implement tenant isolation.

2. **`tenant_id` parameter in `PolicyResolver.resolve()`** — the resolver accepts a
   `tenant_id: str = ""` argument. When a tenant-scoped rule is present in the charter, the
   resolver checks whether `tenant_id` matches the rule's `roles` field (DSL v0 reuses the
   `roles` field as a scope-value list for tenant rules). An empty `tenant_id` matches only
   tenant rules with an empty `roles` list (matches-all semantics).

3. **Tenant-scoped rules in `PermissionsDef`** — the Charter DSL v0 `PermissionRule` model
   (defined in `lionagi/protocols/governance/dsl.py`) accepts `scope: "tenant"` in
   `PermissionRule.scope`. Charter authors can write tenant-scoped allow or deny rules using
   the standard rule schema. The DSL v0 uses the `roles` field to encode the tenant identifier
   list for tenant-scope rules.

4. **`PolicyScope.scope_type == "tenant"`** — in the ADR-0052 `PolicyScope` model, `"tenant"` is
   a valid `scope_type`. A `ScopedPolicy` with tenant scope and `scope_value = "acme"` matches
   operations where `tenant_id == "acme"`. The specificity score of 1 ensures it overrides global
   defaults but is itself overridden by role-scoped and resource-scoped policies.

5. **`OperationContext` carries tenant context** — while `OperationContext` (ADR-0050) does not
   define a `tenant_id` field directly (it captures actor, role, charter, policy release, and
   trace state), the `tenant_id` flows through `PolicyResolver.resolve()` as a resolution
   parameter and is recorded in `PolicyResolutionResult.tenant_id`. An integration can inject
   `tenant_id` into the resolution call via a pre-hook installed on `AgentConfig`.

### What lionagi does not provide

The following are explicitly **not** part of lionagi:

| Capability | Why not included |
|---|---|
| Tenant storage or database isolation | Requires multi-DB infrastructure, migration tooling, and connection pool management. |
| Tenant-aware routing or middleware | HTTP or gRPC middleware that resolves tenant from request headers, JWTs, or subdomains. |
| Tenant billing or metering | Token counting, cost allocation, and invoicing per tenant. |
| Tenant configuration management | API surface for creating, updating, and deleting tenant configurations. |
| Cross-tenant access control enforcement | Preventing one tenant's agents from accessing another tenant's data or tools. Requires isolation that this library does not provide. |
| Tenant provisioning and lifecycle management | Onboarding, suspension, and de-provisioning flows. |
| Tenant-aware caching or state stores | Namespaced cache partitions that enforce tenant boundaries at the infrastructure level. |
| Multi-database routing | Query routing based on tenant identity to isolated data stores. |

The boundary is precisely: lionagi can evaluate a permission rule that says "tenant acme may use
tool X." It cannot enforce that a request truly originates from tenant acme, cannot prevent tenant
beta's data from appearing in an acme evidence chain, and cannot prevent an operator from
misconfiguring the `tenant_id` value passed to the resolver.

**Tenant labels are advisory without isolation infrastructure.** This is the intentional boundary.

---

## Scope

This ADR owns:

- The definition of the tenant scope boundary in lionagi.
- The `ScopeLevel.TENANT = 1` API commitment.

This ADR does not own:

- `ScopeLevel`, `PolicyResolver`, `ResolutionResult` — owned by ADR-0052.
- `OperationContext`, `ServiceContext` — owned by ADR-0050.
- `PermissionRule`, `PermissionsDef`, `CharterDocument` — owned by the Charter DSL v0 spec and
  ADR-0047.

---

## Non-Goals

- **No tenant data model.** There will be no `Tenant` entity, `TenantConfig`, or
  `TenantStore` protocol in `lionagi.protocols` or `lionagi.agent`.

- **No multi-database routing.** lionagi does not route operations to isolated databases based on
  tenant identity. The state model is single-namespace.

- **No tenant-aware caching.** There is no cache partitioning or cache-key namespacing by tenant
  in the session, branch, or DataLogger layers.

- **No billing hooks.** Token consumption tracking and cost allocation are not governance
  primitives. Integrations may add them via the existing hook system.

- **No tenant hierarchy or inheritance.** There is no parent-child tenant relationship, no
  tenant group that inherits from an organization root. The global scope is the broadest level;
  tenant is the next level. No tenant sub-hierarchy exists.

- **No automatic tenant provisioning from charters.** Creating or deleting a "tenant" in lionagi
  means nothing beyond writing or removing tenant-scoped rules in a charter document. There is
  no provisioning side-effect.

---

## Why Tenant Is at Specificity Level 1

The specificity ordering `resource (3) > role (2) > tenant (1) > global (0)` is not arbitrary.
Each level in the hierarchy answers a narrower question:

| Level | Question answered | Example |
|---|---|---|
| global (0) | What is the broadest platform default? | All agents default to read-only tools |
| tenant (1) | What does this organization require above the platform default? | Acme agents may also use write tools |
| role (2) | What does this agent role require above the tenant default? | Reviewer agents within Acme may approve PRs |
| resource (3) | What does this specific tool require above everything else? | `deploy_production` requires JIT grant regardless of role |

Placing `tenant` at level 1 ensures that:

- **Tenant policies override global defaults.** An organization can tighten or relax the
  platform-wide baseline without needing per-role rules for every role. A single tenant-scoped
  allow rule covers all roles in that organization for the specified tools.

- **Role policies override tenant policies.** A tenant's blanket grant does not prevent a
  per-role restriction from being more specific. A `reviewer` role can have stricter constraints
  than the tenant default even within the same organization.

- **Resource policies override everything.** A tool-level gate (ADR-0044) or JIT requirement
  (ADR-0046) governs regardless of what tenant or role policies say. The resource level is the
  ultimate backstop.

If `tenant` were placed at level 0 (equal to global), tenant rules could not override global
defaults without introducing explicit conflict resolution. If placed at level 2 (equal to role),
tenant policies would tie with per-role policies, triggering DENY-on-tie for every deployment
that writes both tenant and role rules for the same tool. Level 1 is the only position that
maintains strict ordering at every resolution step.

**This ordering is a permanent API commitment.** Code that reads `ScopeLevel.TENANT` or
`ScopeLevel.ROLE` must not assume that their relative integer values can change. Any integration
that relies on tenant overriding global but being overridden by role depends on this ordering.
Changing it would be a breaking change requiring a new major version of the governance protocol
and a superseding ADR.

---

## Security Considerations

**Without isolation infrastructure, tenant labels are advisory only.**

A deployment that writes tenant-scoped charter rules but does not install a tenant resolution
middleware will fall back to `tenant_id = ""` (the default in `PolicyResolver.resolve()`). If a
tenant rule has an empty `roles` list (matches-all semantics), it will apply to every operation
regardless of the actual organizational origin. If it has a non-empty `roles` list, it will
never match (since `tenant_id = ""` is not in the list). Neither outcome constitutes a security
isolation guarantee.

**Data leakage across tenant boundaries is possible without isolation infrastructure.**

If two organizations share a lionagi deployment and both use the same DataLogger backend without
tenant-namespace segregation, evidence records from one organization are accessible to agents of
the other. The resolution layer has no mechanism to prevent this. Storage isolation must be
provided by the deployment infrastructure.

**lionagi does not guarantee tenant isolation.** This is a design choice, not a deficiency.
The guarantee boundary is:

> lionagi guarantees that tenant-scoped permission rules are evaluated at specificity level 1
> in the resolution hierarchy. It does not guarantee that the tenant_id value passed to the
> resolver is authentic, that storage is isolated between tenants, or that cross-tenant evidence
> contamination is prevented.

Deployments requiring full tenant isolation must provide additional infrastructure on top of
this library.

**Misconfiguration risk.** An operator who sets `tenant_id = "admin"` in every call — whether
intentionally or by misconfiguration — will match all rules scoped to the `admin` tenant. There
is no HMAC or cryptographic binding between the `tenant_id` string and the session identity.
Authentication of the tenant claim is an infrastructure concern, typically implemented via
verified JWT claims extracted in the tenant middleware hook.

---

## Interfaces And Types

This ADR does not introduce new types. The relevant types are owned by other ADRs:

| Type | Owner | Relevant field |
|---|---|---|
| `ScopeLevel.TENANT` | ADR-0052 | Integer value 1; permanent API commitment |
| `PolicyResolver.resolve(tenant_id=...)` | ADR-0052 | `tenant_id: str = ""` parameter |
| `PermissionRule.scope` | Charter DSL v0 / ADR-0047 | Accepts `"tenant"` as a valid scope value |
| `ResolutionResult.scope_level` | ADR-0052 | Carries `1` when a tenant rule wins |
| `PolicyScope.scope_type == "tenant"` | ADR-0052 | Specificity score = 1 |
| `PolicyResolutionResult.tenant_id` | ADR-0052 | Records resolved tenant in audit result |

The `OperationContext` (ADR-0050) does not carry a `tenant_id` field directly. The tenant is
propagated through the resolution call and captured in `PolicyResolutionResult.tenant_id`, which
is logged via `DataLogger`. An integration that needs `tenant_id` on the evidence node must
stamp it via a post-hook.

---

## Runtime Semantics

The tenant scope evaluation path in `PolicyResolver._collect_candidates()` is:

```python
# From lionagi/protocols/governance/resolution.py
# --- tenant scope (score=1) ---
# Tenant is a scope label only — no isolation, no storage.
score = ScopeLevel.TENANT
for rule in self._allow_by_scope["tenant"]:
    if self._rule_matches_tenant(rule, tenant_id) and self._rule_matches_tool_filter(
        rule, tool_id
    ):
        candidates.append(_Candidate(rule, "allow", int(score)))
for rule in self._deny_by_scope["tenant"]:
    if self._rule_matches_tenant(rule, tenant_id) and self._rule_matches_tool_filter(
        rule, tool_id
    ):
        candidates.append(_Candidate(rule, "deny", int(score)))
```

`_rule_matches_tenant()` is defined as:

```python
@staticmethod
def _rule_matches_tenant(rule: PermissionRule, tenant_id: str) -> bool:
    """A tenant-scope rule matches when ``tenant_id`` is in ``rule.roles``.

    In DSL v0 the ``roles`` field doubles as the scope value list for
    tenant rules (there is no dedicated ``tenants`` field).  Empty list
    matches all tenants.  Providing a non-empty ``tenant_id`` of ``""``
    matches only rules with an empty roles list.
    """
    if not rule.roles:
        return True
    return tenant_id in rule.roles
```

This is the complete implementation of "tenant scope." There is no additional isolation logic,
no storage lookup, no middleware call. The `tenant_id` string is compared against a list of
strings in the charter rule. That is all.

---

## Evidence And Trace Requirements

When a tenant-scoped rule wins resolution, `ResolutionResult.scope_level == 1` and
`ResolutionResult.matching_rule_id` is set to the winning rule's `rule_id`. This is written to
the `DataLogger` via the `to_pre_hook()` integration path (ADR-0052). The justification field
on `ResolutionResult` includes `scope=tenant` in its text.

Integrations that stamp `tenant_id` onto evidence nodes should record it under the key
`metadata.tenant_id` in the `ImmutableEvidenceNode` (ADR-0041) `metadata` dict. This is a
convention, not a schema requirement.

---

## Test Requirements

The test suite must cover:

1. A `PolicyResolver` configured with a tenant-scoped rule resolves ALLOW when `tenant_id`
   matches the rule's `roles` list.
2. A `PolicyResolver` configured with a tenant-scoped rule resolves using the global fallback
   when `tenant_id` does not match and no other rule matches.
3. A `PolicyResolver` with `tenant_id = ""` matches tenant rules with an empty `roles` list
   (matches-all semantics) and does not match tenant rules with a non-empty `roles` list.
4. A tenant-scoped rule at level 1 is overridden by a role-scoped rule at level 2 (most-specific-
   wins — tenant does not win over role for the same tool).
5. A tenant-scoped rule at level 1 overrides a global rule at level 0 (most-specific-wins —
   tenant wins over global).
6. `ScopeLevel.TENANT == 1` — integer value is tested explicitly to catch any inadvertent
   reordering of the enum.
7. The `_rule_matches_tenant` static method is covered by unit tests for both match and non-match
   paths.

These tests must pass before any change to `ScopeLevel`, `PolicyResolver`, or `PermissionRule`
is merged.

---

## Consequences

**Positive**

- The tenant boundary is explicit and formally documented. Operators and integrators can read this
  ADR and understand precisely what multi-tenancy guarantees lionagi makes (none beyond scope-label
  evaluation) and what additional infrastructure is required.

- The API surface for multi-tenancy is stable and minimal. An integration requires no forking of
  the core codebase — only hook installation and a storage backend implementation.

- Existing single-tenant deployments are not affected. The `tenant_id` parameter defaults to
  `""`, and the resolution algorithm behaves identically to a deployment with no tenant-scoped
  rules.

- The `ScopeLevel.TENANT = 1` commitment protects integrators from a future protocol change that
  would break tenant-policy ordering assumptions.

- The documentation of the advisory-only security guarantee prevents false assurance.

**Negative**

- Operators who skim the charter DSL documentation may assume that writing `scope: "tenant"` in
  a rule provides isolation. They will not get isolation. This ADR's existence is the primary
  mitigation; the charter DSL documentation must link to it.

- The `roles` field reuse for tenant-scope matching in DSL v0 is a known awkwardness. A
  dedicated `tenants` field in DSL v1 would be cleaner. DSL v0 is the current standard; this
  debt is tracked for the DSL v1 design cycle.

---

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Implement basic tenant isolation (e.g. separate SQLite per tenant) | Introduces infrastructure complexity with no value for single-tenant users. Creates a maintenance burden with no corresponding benefit for most deployments. |
| Remove tenant scope from resolution hierarchy entirely | Forces integrations that need tenant-level policy to fork the resolution engine. Breaks the interoperability surface. Creates two incompatible resolution algorithms. Rejected because the label costs nothing to keep and enables interoperability. |
| Rename `TENANT` to `ORG` or `NAMESPACE` to avoid isolation connotations | "tenant" is the industry-standard vocabulary for multi-tenancy. The ADR documentation makes the advisory-only semantics clear without needing to rename the concept. |
| Place tenant at level 2 (same as role) | Causes DENY-on-tie for any deployment that writes both a tenant rule and a role rule for the same tool at the same specificity. Breaks the useful pattern of "tenant sets a baseline, role refines it." Rejected because it makes the most common multi-tenant pattern impossible without resource-level overrides. |
| Place tenant at level 0 (same as global) | Tenant rules cannot override global defaults, which removes the primary value of the tenant scope level. A tenant rule that cannot narrow or expand the global default is useless. Rejected. |

---

## Cross-References

- [ADR-0052](ADR-0052-policy-resolution.md) — defines `ScopeLevel`, `PolicyResolver`, and the
  most-specific-wins algorithm. `ScopeLevel.TENANT = 1` is the numerical commitment this ADR
  formalizes.
- [ADR-0050](ADR-0050-operation-context.md) — `OperationContext` carries the active policy
  release. `PolicyResolutionResult.tenant_id` is the trace field that records the resolved
  tenant in evidence.
- [ADR-0047](ADR-0047-agent-charter.md) — the charter is the session-binding vehicle. Tenant-
  scoped rules appear in `CharterDocument.permissions`.
- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — `ImmutableEvidenceNode` is the target for
  `metadata.tenant_id` stamping when needed by an integration.
- [ADR-0044](ADR-0044-tool-gates.md) — resource-scoped gate enforcement (specificity 3) takes
  precedence over tenant-scoped rules (specificity 1).
- [ADR-0046](ADR-0046-jit-tool-grant.md) — JIT grants are policy-scoped; tenant context flows
  through `PolicyResolver` to the grant evaluation.
- `lionagi/protocols/governance/resolution.py` — canonical implementation of `ScopeLevel`,
  `PolicyResolver`, and `_rule_matches_tenant`.
- `lionagi/protocols/governance/dsl.py` — `PermissionRule.scope` accepts `"tenant"`;
  `_rule_matches_tenant` behavior follows from the `roles` field semantics.
