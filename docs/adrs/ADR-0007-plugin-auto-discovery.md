# ADR-0007: Plugin Manifest Layout and Auto-Discovery Convention

**Status**: Accepted
**Date**: 2026-05-19

## Context

Each of lionagi's four marketplace plugins (see ADR-0003) has a `plugin.json` manifest under
`.claude-plugin/`. The Claude Code plugin loader must discover which skills and agent profiles
each plugin provides. Two approaches exist: explicit enumeration (listing files in `plugin.json`)
or implicit auto-discovery from directory layout.

The khive marketplace (`gtd` and `kg` plugins) has validated the auto-discovery path: `plugin.json`
carries metadata (name, description, version) but no `skills` or `agents` arrays; the CC loader
discovers those by scanning `skills/` and `agents/` subdirectories.

## Decision

`plugin.json` for every lionagi marketplace plugin does NOT enumerate `skills` or `agents` arrays.
The Claude Code plugin loader auto-discovers skill and agent files from the directory layout:

```text
marketplace/<plugin>/
  .claude-plugin/plugin.json   # metadata only — no skills/agents arrays
  skills/                      # auto-discovered by CC loader
  agents/                      # auto-discovered by CC loader
```

This follows the khive `gtd`/`kg` precedent. Deviation from this convention (adding explicit
arrays) would create a redundancy that lags the actual directory contents.

## Consequences

**Positive**

- Adding a skill file to `skills/` is immediately effective; no manifest update required.
- Manifest stays small and readable — metadata only.
- Consistent with the only production reference implementation available at time of decision.

**Negative**

- Auto-discovery behavior is undocumented by Anthropic and may change before GA. If the CC
  loader stops auto-discovering from directory layout, all four plugins break silently — there
  is no explicit manifest to fall back on.
- This ADR is the only record of the assumption. Consumers who read only `plugin.json` will
  not see the skill/agent inventory.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Explicit `skills` and `agents` arrays in `plugin.json` | Redundant with directory layout; would drift from actual content on every skill addition/removal |

## References

- `_show.md:111` — decision confirmed: `plugin.json` stays as-is; CC loader auto-discovers
- `marketplace-plugins-core/_intent.md:49` — acceptance item: CC auto-discovery confirmed
- khive marketplace reference: `/Users/lion/projects/khive/khive/marketplace/`
- [ADR-0003](ADR-0003-claude-code-marketplace.md) — marketplace structure this decision extends
