# ADR-0074: Role Composition & Pack-Based Per-Role Configuration

**Status**: Proposed
**Date**: 2026-06-01

## Context

The orchestration clean break (the rewrite onto casts emissions; see ADR-0072)
made the CLI plan a `list[TaskAssignment]` and workers casts roles: the
orchestrator names an `assignee` role, and `build_worker_branch` composes that
role's system prompt. The intent was for the 40 built-in casts roles to become
the behavioral library that orchestration draws on.

A live `li o flow` run (2026-06-01, `codex/gpt-5.3-codex-spark`) exposed three
reconciliation gaps that block that intent:

1. **Profiles shadow casts roles.** Resolution is profile-wins-by-name: where a
   `~/.lionagi/agents/<name>.md` exists, its body and model win and the casts
   role body is never rendered. In the run, `analyst` resolved to the profile's
   `codex/gpt-5.5`, while `assessor` (no profile) used the casts role body. So
   the casts library is bypassed exactly where a user has invested in profiles,
   and there is no way to use a casts role *and* pin a model without writing a
   whole prompt file.
2. **Modes are unwired.** The fourteen cognitive modes (ADR-0071) never reach
   orchestration: `casts_role_system` calls `AgentSpec.compose(role)` with no
   `modes=`, and `TaskAssignment` has no mode field. ADR-0071 itself deferred
   both the role→mode allowlist and any selection mechanism.
3. **Selection does not scale.** With 40 roles × 14 modes, dumping every option
   into the orchestrator prompt gives it no principled basis to choose — the
   same problem ADR-0071 named for modes ("no principled basis for selecting").

## Decision

### Three composition axes, three binding times

| Axis | Question | Source | Bound |
|------|----------|--------|-------|
| **Role** | what you *do* | casts `Role` (closed set) | compose-time |
| **Mode** | *how* you reason | casts `Mode` (closed set) | compose-time overlay |
| **Domain** | what you *know* | LORE (25K domains, `mcp__lore`) | **run-time, retrieved** |

Knowledge is *not* a fourth thing baked into the persona. The old khive
`agent_composer` merged role + domain knowledge into one static prompt; that
does not scale to a 25K-domain corpus. Instead a worker pulls domain knowledge
*at run-time* via `lore suggest/compose` for its specific subtask. Roles + modes
form a small static persona; knowledge is fetched on demand.

### The Pack is the per-role configuration layer

`Pack`/`RolePolicy` is already the user-extensible per-role overlay (it carries
`authority`/`boundaries`/`escalations` per role, "for a future orchestrator to
consume"). Extend it — do not add a parallel `settings.json` or
`~/.lionagi/roles/` directory — to also carry per-role runtime configuration:

```yaml
# a pack file
roles:
  researcher:
    model: codex/gpt-5.5
    effort: high
    default_modes: [evidential]
    modes_allow: [evidential, systematic, probabilistic]   # ADR-0071 role→mode allowlist
    active: true                                            # roster membership
    authority: [...]          # existing RolePolicy fields unchanged
    escalations: [...]
  critic:
    default_modes: [adversarial, premortem]
    model: codex/gpt-5.5
    effort: high
```

The pack becomes the single home for per-role `{policy + model + effort + mode
defaults + roster membership}`. This is what makes packs genuinely useful.

### Resolution precedence — each layer optional and narrower

```text
casts Role body            (behavior — always present)
  → Pack role-config       (model · effort · default_modes · modes_allow · active · policy)
    → Profile              (.lionagi/agents/* — custom body; SHOULD declare role:/modes: to COMPOSE)
      → per-task override   (orchestrator-chosen modes / model on the TaskAssignment)
```

This inverts the current default: casts roles are now the base everywhere, the
pack tunes their runtime, profiles become a *thin overlay* (and, by declaring
`role:`/`modes:` frontmatter, compose with casts via `AgentSpec.compose` instead
of shadowing it). A profile with no `role:` keeps today's raw-body behavior —
back-compat preserved.

### Mode wiring

- `casts_role_system` composes `AgentSpec.compose(role, modes=default_modes)`
  where `default_modes` comes from the pack.
- `TaskAssignment` gains an optional `modes: list[str]` — the orchestrator's
  task-adaptive override, validated against the role's `modes_allow`
  (ADR-0071's deferred role→mode allowlist, now living in the pack).

### Selection from many

Split the decision so no single point drowns in options:

- **Role** — the orchestrator picks, but only from a **curated active set**
  (pack `active:` membership / tiering), not all 40. Tight menu + dense casts
  `description`s make this tractable.
- **Mode** — defaults per role from the pack; the orchestrator overrides *only*
  when it has a specific reason. Its decision space stays "which role," not
  "which of 14 modes."
- **Domain** — out of the orchestrator's scope entirely; resolved by the worker
  at run-time via lore retrieval.

## Consequences

**Positive**

- Casts roles become the actual default and are used everywhere, not just for
  un-profiled names.
- Modes finally compose into orchestration, with per-role defaults and a bounded
  override — closing the two items ADR-0071 deferred.
- The Pack becomes the genuinely useful configuration keystone (policy, runtime,
  mode defaults, roster), with no parallel config location.
- Profiles shrink to the rare "I need a fully custom prompt body" case and can
  compose casts rather than shadow it.
- The orchestrator's decision space is bounded (roles from a curated set);
  knowledge scales via retrieval.

**Negative**

- Pack schema grows (`model`, `effort`, `default_modes`, `modes_allow`,
  `active`); `Pack.from_file` and `RolePolicy` must be extended.
- `TaskAssignment` gains an (additive, optional) `modes` field.
- Existing profiles keep shadowing until migrated to declare `role:`/`modes:`;
  needs a short migration note, but no breakage.
- The curated active-set requires maintenance as the role roster evolves.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep profile-shadows-cast (current) | Casts library bypassed wherever a profile exists; modes orphaned; cannot pin a model to a casts role without writing a whole prompt file. |
| New `settings.json` / `~/.lionagi/roles/<role>.yaml` dir | Duplicates what `Pack` already is (the per-role overlay). A directory of files is heavy for `{model, effort, modes}`; reserve files for body overrides (= profiles). |
| Casts-only; deprecate profiles | Breaks every existing user profile; removes the custom-body escape hatch. |
| Orchestrator selects modes from all 14 per task | Choice paralysis — exactly the "no principled basis for selecting" problem ADR-0071 named. Per-role defaults + bounded override is cheaper and better. |
| Static role+domain merge (old khive `agent_composer`) | Does not scale to a 25K-domain lore corpus; run-time retrieval is the right model for the knowledge axis. |

## References

- ADR-0071 — Cognitive Mode Model (modes; deferred role→mode allowlist + selection).
- ADR-0072 — Reactive Capability Bus (the emission/orchestration substrate).
- ADR-0073 — Universal Agent Spec (`AgentSpec.compose`/`from_yaml`, the composition surface).
- `lionagi/casts/{pattern,profile,pack}.py`; `lionagi/casts/packs/default.yaml`.
- `lionagi/cli/orchestrate/_orchestration.py` — `casts_role_system`, `role_roster`, `build_worker_branch`.
- `lionagi/agent/spec.py` — `AgentSpec.compose(role, modes=, model=, effort=)`, `from_yaml`.
- Live evidence: `li o flow` run 2026-06-01 (`analyst`→profile, `assessor`→cast; codex skip-git-repo-check default bug surfaced and fixed).
- Prior art: khive `services/composition/agent_composer.py` (role + domain static merge, corrected here for retrieval).
