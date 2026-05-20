---
name: bridge-design
description: Forward-looking design notes for bridging lionagi runs and khive's knowledge graph. Read before implementing the bridge.
allowed-tools: [Read, Grep, Glob]
---

# bridge-design

## Status

Placeholder — implementation pending.

## What this plugin will become

A bridge from lionagi runs/agents — which produce structured artifacts under
`~/.lionagi/runs/<id>/` — to khive's knowledge graph.

khive's KG uses a **closed** schema:

- **Entity kinds** (6): `concept` | `document` | `dataset` | `project` | `person` | `org`
- **Edge relations** (13): `contains` | `part_of` | `instance_of` | `extends` | `variant_of` |
  `introduced_by` | `supersedes` | `depends_on` | `enables` | `implements` |
  `competes_with` | `composed_with` | `annotates`

Ad-hoc kinds or relations are rejected at compile time — map to the canonical set or do not link.

## Integration shape (proposed)

After a lionagi run completes, an emit hook:

1. Reads `~/.lionagi/runs/<id>/` artifacts (markdown, JSON, kpp).
2. Extracts named concepts: paper citations → `document`, techniques → `concept`,
   code modules → `project`, authors → `person`.
3. Creates entities via `mcp__khive__create`:
   ```
   create(kind="entity", entity_kind="concept", name="<canonical name>",
          description="<1-2 sentence summary>",
          properties={"domain": "...", "source_run": "<id>"})
   ```
4. Links entities via `mcp__khive__link`:
   ```
   link(source_id="<from>", target_id="<to>", relation="<relation>", weight=<0.4-1.0>)
   ```

**Confidence thresholds**: write at 0.7+; auto-link at 0.8+.
Edge weight bands: 1.0 = definitional, 0.7–0.9 = strong, 0.4–0.6 = plausible.

## Why this is a separate plugin

lionagi can run without khive, and khive can run without lionagi. The bridge is
opt-in — installing `kg-bridge` is the only wiring required. Neither core package
gains a mandatory dependency on the other.

## When implemented

This skill splits into two:

- **bridge-emit** — post-run hook that reads `~/.lionagi/runs/<id>/` and emits
  entities + edges to khive.
- **bridge-recall** — pre-run hook that queries khive and injects relevant entities
  into the run's context before the first branch executes.

For now, this `SKILL.md` is the design contract. Consult it before starting the
implementation so the two halves stay consistent.
