# play

Playbook authoring for lionagi orchestration flows (`li play` / `li o flow`).

## What's inside

- **skills/write-playbook** — scaffolds a new lionagi playbook: title, shape, roles, steps, and artifact protocol

## Install

```
claude /plugin marketplace add ohdearquant/lionagi
claude /plugin install play@lionagi
```

## Quick start

```
/write-playbook <name>
```

Guides you through authoring a complete lionagi playbook for `li play` or `li o flow`,
producing a spec-complete YAML/markdown playbook in the current directory.

## See also

- ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
