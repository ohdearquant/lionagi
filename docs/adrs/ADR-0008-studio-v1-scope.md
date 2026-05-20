# ADR-0008: Lion Studio Scope — CLI-Primary, Definition-Editable, Localhost

**Status**: Accepted
**Date**: 2026-05-19 (revised 2026-05-20)

## Context

Lion Studio is a dashboard for observing, inspecting, editing, and debugging the
lionagi runtime. Before building features, the scope must be bounded to prevent
consumer-SaaS feature creep.

The governing principle is **CLI-primary, Studio-secondary** (ADR-0014): the CLI
handles creation and execution; Studio handles observation, inspection, editing
definitions, and debugging.

## Decision

### What Studio does

| Capability | Scope |
|-----------|-------|
| **Observe** | Dashboard metrics, session list, show progress, plugin catalog |
| **Inspect** | Run detail with branches/messages/errors/files, show play details |
| **Edit** | Agent definitions, playbook YAML — with version history and rollback |
| **Debug** | Execution lineage drill-down: show → play → session → branch → messages |
| **Browse** | Plugin/skill/agent catalog with source information and cross-links |

### What Studio does NOT do

| Capability | Why not | Where instead |
|-----------|---------|---------------|
| Run playbooks with parameters | CLI handles input binding, worktree setup, team coordination | `li play` |
| Create agents from scratch | Primary authoring is text editor + CLI | `$EDITOR` + `li agent` |
| Install/remove plugins | Claude Code manages plugin lifecycle | `claude plugin add/remove` |
| Orchestrate shows | Show skill is a Claude Code agent skill | `/show topic` in Claude Code |
| Authentication/RBAC | Localhost-only, single-user | Not needed |
| Multi-workspace/remote backends | Single local machine | Not needed |
| Rich execution configuration | Parameters, team mode, worktree customization | CLI flags |

### The "Run" button exception

The playbook detail page has a "Run" button — a convenience shortcut that shells
out to the CLI with defaults. It is explicitly de-emphasized and does not support
input binding, execution modes, or team configuration.

### Write policy

- **Writable**: agent definitions, playbook definitions (through definitions API
  with version history)
- **Read-only**: plugin components (marketplace and third-party), skills, session
  data, show data, run data
- **Import-only**: filesystem runs → SQLite sessions (via `li state import`)

## Consequences

**Positive**
- Studio stays focused: observe + inspect + edit + debug.
- No feature creep toward replicating CLI capabilities in the browser.
- Simpler UI: no complex forms for run configuration.
- The CLI evolves independently — new execution modes don't require Studio changes.

**Negative**
- Collaborators who don't use the CLI cannot create or run things from Studio alone.
- The Run button is a partial exception that may confuse expectations.
- Some inspector features (e.g., "re-run this failed play") require switching to
  the terminal.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Full execution surface | Replicates CLI complexity; two execution surfaces means two places for bugs |
| Read-only only (no editing) | Editing definitions with version history is genuinely useful |
| Ship with auth | Localhost-only workload; adds config surface for no benefit |
| Multi-workspace support | Single-user local tool; adds routing/state complexity for no benefit |
